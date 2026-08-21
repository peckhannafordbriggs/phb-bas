# BAS Pipeline — Runbook

**For whoever is looking after this, including someone who has never seen the code.**

---

## What this is, in one paragraph

Building automation data lives on a Niagara controller (a JACE) that only keeps about **42 hours** of history before overwriting it. This system copies that data into a PostgreSQL database before it's destroyed, so it can be charted, queried, and analysed. A collector runs every 15 minutes. Grafana shows trends. Claude Desktop can answer questions about it.

**The one thing to internalise:** past 42 hours, this database is the *only* copy of that building's history in existence. Niagara has already overwritten its own. There is nothing to re-import from.

---

## Where everything lives

| | |
|---|---|
| Database | PostgreSQL 17, local, database `bas`, schema `bas` |
| Collector | `C:\dev\bas-collector` |
| Schema/migrations | `C:\dev\bas-db` |
| Claude integration | `C:\dev\bas-mcp` |
| Dashboards | `C:\dev\bas-grafana`, Grafana at http://localhost:3000 |
| Lab station | `196.1.1.213` — JACE-9000, station name `SpringGroveLabComputer` |
| Logs | `C:\dev\bas-collector\logs\` |

### Accounts

| Account | Where | Purpose |
|---|---|---|
| `bas` / `bas_local_dev_only` | Postgres | Owns the schema, used by the collector to write |
| `bas_readonly` / `bas_readonly_local` | Postgres | Grafana and Claude. Cannot write anything |
| `postgres` / *(not recorded — see Recovery)* | Postgres | Superuser. Needed to create roles |
| `bas_collector` / *(set in Workbench)* | Niagara station | Read-only. Cannot change anything on the station |
| `admin` | Grafana | Local only |

> **`postgres` password is not written down anywhere.** That's a known gap. See *Recovery → Lost postgres password*.

---

## Is it working?

Three ways, quickest first.

**1. The dashboard.** Grafana → *BAS — Collection Health*. Look at **"Minutes since newest reading."** Green under 30 is fine. Red over 60 means collection has stopped.

**2. The command.**

```powershell
cd C:\dev\bas-collector
python healthcheck.py
```

Prints `OK`, `WARNING`, or `CRITICAL` with an explanation. This is what runs automatically every 30 minutes.

**3. Are the scheduled tasks alive?**

```powershell
Get-ScheduledTask 'BAS*' | Get-ScheduledTaskInfo | Format-Table TaskName, LastRunTime, LastTaskResult
```

`LastTaskResult` of `0` means the last run succeeded.

---

## Symptom → cause → fix

### Collection has stopped

**Symptom:** "Minutes since newest reading" climbing past 60. Health check says `Collection stalled`.

**This is urgent.** At 42 hours the station starts destroying data we haven't collected.

Work through in order:

```powershell
# 1. Is the task even there and enabled?
Get-ScheduledTask 'BAS Collector Sync'

# 2. Run it by hand and read the error
cd C:\dev\bas-collector
python -m collector check
```

| `check` says | Do this |
|---|---|
| Database FAIL | `Get-Service postgresql*` — if stopped, `Start-Service postgresql-x64-17` |
| Station 401 | See *Station rejects login* below |
| Station timeout | Network path to the JACE is gone. Can you browse to `https://196.1.1.213`? |
| Everything OK | The task isn't firing. Re-run `Install-BasTasks.ps1` as administrator |

Once fixed, catch up immediately — don't wait for the schedule:

```powershell
python -m collector sync
```

### Station rejects login (401)

**Almost never a wrong password.** Niagara users default to a digest authentication scheme that HTTP clients can't satisfy. If a 401 appears suddenly, the likely causes are:

- The `bas_collector` account was disabled or deleted on the station
- Its **Authentication Scheme Name** was changed off `HTTPBasicScheme`
- Its **role** (`pipelinereader`) was removed — this presents as 401/403 and looks like an auth failure

Check in Workbench: `Config → Services → UserService → bas_collector`. It needs `HTTPBasicScheme`, the `pipelinereader` role, Enabled = true, and Auto Logoff **disabled**.

> That account cannot log into Workbench or the station web UI. That's correct and by design — it's a machine account.

### Station returns 404 on /obix/

The oBIX servlet has stopped serving. Check `Config → Drivers → ObixNetwork` still exists in the station. If a station was restored from an older backup, it may have been removed.

If the ObixNetwork is gone, re-add it from the `obixDriver` palette (see *Adding a building*, step 3).

### A data gap appeared

**This is data that no longer exists anywhere.** Nothing can recover it. What matters is stopping it recurring.

```powershell
psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -c "SELECT p.display_name, g.gap_start, g.gap_end, g.cause FROM bas.data_gap g JOIN bas.point p USING (point_id) ORDER BY g.gap_start DESC LIMIT 20"
```

| Cause | Meaning |
|---|---|
| `roll_overwrite` | Collector was down longer than 42 hours. Find out why and fix it |
| `station_unreachable` | Network or station outage |
| `collector_down` | Machine off, task disabled, or process crashed |

Then check whether the poll interval is still safe:

```powershell
python -m collector status
```

If it reports points as `at_risk`, either poll more often (`POLL_INTERVAL_S` in `.env`) or increase the history capacity on the station in Workbench.

### Grafana panel is blank or shows a permissions error

Usually a new migration created a table or view that `bas_readonly` can't read yet.

```powershell
psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -c "GRANT SELECT ON ALL TABLES IN SCHEMA bas TO bas_readonly"
```

If it keeps happening, the default privileges are wrong — re-run `C:\dev\bas-mcp\setup_readonly_role.sql` as the `postgres` superuser.

### Claude Desktop can't see the BAS tools

1. **Settings → Developer** — is `bas` listed?
2. If not, the config JSON is probably malformed. It's at:
   `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
   Backslashes in paths must be **doubled**.
3. **Fully quit Claude** — `Get-Process Claude* | Stop-Process -Force` — then reopen. Closing the window isn't enough.
4. Test the server directly, which gives a far clearer error than Claude does:

```powershell
cd C:\dev\bas-mcp
$env:BAS_READONLY_URL="postgresql://bas_readonly:bas_readonly_local@localhost:5432/bas"
python test_tools.py
```

### Points report `roll_horizon_unknown`

Not a fault — it means `capacity` and `collection_interval_s` haven't been filled in for those points, so we **cannot tell** whether the poll interval is safe. Unknown is not the same as safe.

Get the values from Workbench (*History Ext Manager* on the device), then:

```sql
UPDATE bas.point SET capacity = 500, collection_interval_s = 300, full_policy = 'roll'
WHERE niagara_history_name = '<exact name>';
```

oBIX does not expose these. They only exist in Workbench or via BQL.

### Disk filling up

```powershell
psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -c "SELECT pg_size_pretty(pg_database_size('bas'))"
```

Roughly 10 GB per building per three years. If it becomes a problem the answer is monthly partitioning on `bas.reading`, not deleting data — deleted history cannot be recovered.

---

## Adding a building

**Preferred route: import into the existing central station.** No firmware upgrade, no downtime on the production controller.

**What you need from whoever owns the JACE:** its IP, confirmation the central station can reach it, and a read-only user account on it.

1. **On the central station** (Workbench → `Config → Drivers → NiagaraNetwork`) add the JACE as a station using that read-only account.
2. **Configure history imports** so its trend data copies across.
3. **Verify the histories arrived** — this is the check that matters:

   ```
   https://196.1.1.213/obix/histories/
   ```

   You should see a **new station folder** appear alongside `SpringGroveLabComputer`. If everything instead appears flattened under the existing station, stop and flag it — point identity depends on that naming and it needs sorting before data accumulates.

4. **Register the new points:**

   ```powershell
   cd C:\dev\bas-collector
   python -m collector discover
   python -m collector sync
   ```

5. **Fill in capacity / interval** from Workbench for the new points, or the roll guard can't protect them.

6. **Classify the points** — assign `point_role` and `equipment_id`. Unclassified points are invisible to every role-based query and most fault rules.

> **Direct-to-JACE is the fallback**, and it's much heavier: the controller needs the `obixDriver` runtime module installed, which requires **commissioning** — a full kernel/OS/JVM firmware upgrade and reboot. On a live building that's a scheduled maintenance window. Back up the station with Station Copier first.

---

## Recovery

### Restore the database

If backups are running (see below):

```powershell
psql -U postgres -c "DROP DATABASE bas"
psql -U postgres -c "CREATE DATABASE bas OWNER bas"
psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -f C:\backups\bas\<latest>.sql
```

**If there is no backup, the data is gone.** The station only has the last 42 hours. This is why backups matter more here than in most systems.

### Set up backups (do this before real data accumulates)

```powershell
# Manual
pg_dump "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -f "C:\backups\bas\bas_$(Get-Date -f yyyyMMdd).sql"
```

Schedule it daily, and **put the output somewhere other than this machine's disk** — a network share or OneDrive. A backup on the same drive as the database protects against nothing that actually happens.

### Restore the station

A copy of the lab station was taken before the firmware upgrade:

```
C:\Users\Msheth\Niagara4.15\iSMA CONTROLLI\stations\SpringGroveLabComputerCopy
```

Push it back with Workbench → Platform → **Station Copier**, local → remote.

### Lost postgres password

Currently not recorded anywhere. To reset:

1. Find `pg_hba.conf` (usually `C:\Program Files\PostgreSQL\17\data\`)
2. Change the `host all all 127.0.0.1/32 scram-sha-256` line to `trust`
3. `Restart-Service postgresql-x64-17`
4. `psql -U postgres -c "ALTER USER postgres PASSWORD 'newpassword'"`
5. **Change `pg_hba.conf` back** and restart again

Needs administrator. Do not leave it on `trust`.

### Lost Niagara service account password

Don't recover it — set a new one. Workbench → `Config → Services → UserService → bas_collector` → set a new password, then update `NIAGARA_PASS` in `C:\dev\bas-collector\.env`.

---

## Routine maintenance

| When | What |
|---|---|
| Weekly | Glance at the Collection Health dashboard |
| Monthly | Confirm a backup actually restores. An untested backup is a guess |
| Monthly | `python -m collector discover` — picks up new or renamed points |
| When points are added | Classify them, or they're invisible to analysis |
| Annually | Check the Niagara SMA date (currently current to **2027-04-21**) |

---

## Things that are deliberate, not bugs

**The collector cannot write to the station.** Read-only account, and the client refuses non-GET requests before opening a socket. If someone asks for write-back — setpoint changes, overrides — that is a separate tool with separate credentials and a human confirmation step, not a change to this one.

**Claude cannot modify the database.** Three independent layers. If a query fails with a permissions error, that's the design working.

**Renaming a point in Niagara creates a new point here.** The old one is marked inactive, not deleted, and keeps its history. That's intentional — the alternative silently changes the meaning of past data.

**A row with no value is not missing data.** It means the station reported a null — a sensor fault or a genuine gap. Different from no row at all, which means we never collected it. `bas.data_gap` records the second kind.

---

## Known gaps

- **No off-machine backup yet.** The highest-priority thing to fix before real data accumulates.
- **Runs on a laptop.** Sleeps, travels, gets closed. Needs an always-on company machine before anyone depends on it.
- **`postgres` password not recorded.**
- **TLS verification disabled** (`NIAGARA_VERIFY_TLS=0`). The certificate fingerprint is logged on every discovery run — pin it instead.
- **Adding a building requires a terminal.** No UI.
- **Alerting is local only** — desktop notification and event log. No email unless `BAS_ALERT_TO` and `BAS_SMTP_SERVER` are configured, which M365 usually blocks without an app password.
