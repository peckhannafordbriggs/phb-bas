# BAS Pipeline — Runbook

**For:** whoever is operating this, including someone who has never seen the code.
**Updated:** 20 August 2026

If you read nothing else, read the next two boxes.

---

## The one thing that makes this different from normal software

**The data in this database is the only copy that exists.**

The JACE keeps roughly 42 hours of history and then overwrites it. Once a reading is older than that, the controller no longer has it. If it is deleted here, it is gone — there is no source to re-import from.

Consequences:

- **Never DELETE from `bas.reading`.** No exceptions, no "just this once."
- **Back the database up.** See *Backups* below. This is not optional the way it is for a system you could rebuild from source.
- **Every hour the collector is down is an hour closer to permanent loss.** After ~42 hours down, you have lost data forever.

## If something is wrong right now, start here

```powershell
cd C:\dev\bas-collector
python -m collector check
```

That checks configuration, the database, and the station in one go, and tells you which one is broken. Almost every problem below is diagnosed by that command.

---

## What the pieces are

| Piece | Where | What it does |
|---|---|---|
| **Niagara station** | JACE at `196.1.1.213` | The building controller. Holds ~42h of trend history, serves it over oBIX |
| **Collector** | `C:\dev\bas-collector` | Python. Polls the station every 15 min, writes to Postgres |
| **Database** | Postgres on localhost | Where the data lives. **The only permanent copy** |
| **MCP server** | `C:\dev\bas-mcp` | Lets Claude Desktop answer questions about the data |
| **Grafana** | `http://localhost:3000` | Dashboards |

```
JACE ──oBIX/HTTPS──> collector ──> PostgreSQL ──┬──> Claude (via MCP)
                                                 └──> Grafana
```

Data flows one way. The collector is **read-only against the station** — enforced in code, and enforced again by the `bas_collector` Niagara account having no write permission. Nothing in this system can affect building control.

---

## Failure modes

### Data has stopped arriving

**Symptom:** Grafana's "Minutes since newest reading" climbs past 30. Claude says data is stale. `collector status` shows old timestamps.

**This is the urgent one** — the clock is running on permanent data loss.

**Check in this order:**

**1. Is the collector running?**

```powershell
Get-Process python -ErrorAction SilentlyContinue
```

If nothing, it stopped. Restart it:

```powershell
cd C:\dev\bas-collector
python -m collector run
```

**2. Can it reach the station?**

```powershell
python -m collector check
```

- `Could not connect` → network or the JACE is down. `ping 196.1.1.213`
- `401 Unauthorized` → see *Authentication broke*
- `404` → see *oBIX stopped serving*

**3. Is the database up?**

```powershell
Get-Service postgresql*
```

If stopped: `Start-Service postgresql-x64-17`

**4. Catch up once it's fixed.** Nothing needed — the collector resumes from its checkpoint automatically and backfills whatever the station still holds. Anything older than ~42 hours is gone and will be recorded in `bas.data_gap`.

---

### Authentication broke

**Symptom:** `401 Unauthorized` from `collector check`.

**Almost never a wrong password.** In order of likelihood:

1. **A stale cookie in `.env` is overriding the username.** The collector prefers cookie auth when both are present.

   ```powershell
   Get-Content C:\dev\bas-collector\.env | Select-String "^NIAGARA_(USER|COOKIE)"
   ```

   `NIAGARA_COOKIE=` must be empty. `NIAGARA_USER=bas_collector` must have no space after the `=`.

2. **The `bas_collector` account lost its scheme or role.** In Workbench: Config → Services → UserService → `bas_collector`. Confirm **Authentication Scheme Name = `HTTPBasicScheme`** and **Roles = `pipelinereader`** only.

3. **The account was disabled or deleted.** Same screen — `Enabled` should be true.

**Background:** Niagara users default to a digest scheme that HTTP clients cannot satisfy. `HTTPBasicScheme` is the only one that works for a machine client, and an account using it **cannot log into Workbench or the web UI**. That is correct, not broken.

---

### oBIX stopped serving

**Symptom:** `404` on `/obix/`. Confirm in a browser: `https://196.1.1.213/obix/`

**Cause:** the `ObixNetwork` component was removed from the station, or the station was restored from a backup predating it.

**Fix:** Workbench → Palette → open `obixDriver` → drag **ObixNetwork** onto **Config → Drivers**.

**If that fails with "missing modules required: obixDriver-rt"**, the runtime module is gone from the host and must be reinstalled through Platform → Software Manager. **That triggers commissioning, which is a full firmware upgrade and reboots the controller.** Do not do that casually on a production JACE — it needs a maintenance window.

---

### The collector refuses to run

**Symptom:** `REFUSED: N point(s) cannot be safely collected at a 900s poll interval`

**This is the guard working, not a bug.** A point's history capacity is too small for the poll interval, so records would be overwritten before collection.

**Fix, in order of preference:**

1. Lower `POLL_INTERVAL_S` in `.env`
2. Raise the history capacity on the station (Workbench → History Ext Manager)
3. Only if you have consciously decided the loss is acceptable: `ENFORCE_ROLL_GUARD=0`

**Do not reach for option 3 first.** The failure it prevents is silent and permanent — Niagara overwrites with no error, no log entry, and no gap marker anywhere.

---

### Everything runs but the numbers look wrong

**Stop collecting and investigate.** Bad data is worse than no data, and it compounds.

1. Pick one point and one time window
2. Open the same history in Workbench for the same window
3. Compare timestamps and values exactly

If they disagree, tell whoever maintains this before collecting more. That comparison is the correctness anchor for the whole system.

---

### Claude can't see the data

**Symptom:** `bas` missing from Claude Desktop's tool list, or tools error.

1. **Settings → Developer** — is `bas` listed?
2. **Fully quit Claude** — right-click the tray icon and Quit, not just close the window. It only reads its config on a cold start.

   ```powershell
   Get-Process Claude* | Stop-Process -Force
   ```

3. **Test the server directly** — the error will be far clearer than what Claude shows:

   ```powershell
   cd C:\dev\bas-mcp
   $env:BAS_READONLY_URL="postgresql://bas_readonly:bas_readonly_local@localhost:5432/bas"
   python test_tools.py
   ```

   Expect `21 passed, 0 failed`.

4. **Config file** is at
   `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`.
   A single missing comma makes it invalid and the server silently won't load.

---

## Routine tasks

### Add a new building

**Preferred route — import via the central station.** No firmware work, no downtime on the production controller.

1. On the new JACE: create a read-only user (see *Create a service account*)
2. On the central station, Workbench → Config → Drivers → NiagaraNetwork → add the JACE as a station
3. Configure history imports for the points you want
4. **Check the histories appear under their own station name:**
   `https://196.1.1.213/obix/histories/` should show a *new* station folder
5. `python -m collector discover`
6. `python -m collector sync`
7. Fill in capacity and collection interval (see below)
8. Assign `point_role` and `equipment_id`

**Direct route** — only if importing isn't possible. Requires installing `obix-rt` and `obixDriver-rt` on that JACE via Platform → Software Manager, **which triggers a firmware upgrade and reboot.** Schedule it.

### Create a service account on a station

In Workbench, on that station:

1. Config → Services → AuthenticationService → confirm `HTTPBasicScheme` exists. If not, add it from the `baja` palette under `AuthenticationSchemes/WebServicesSchemes`
2. Config → Services → RoleService → New role, **read permissions only** — no write, no invoke
3. Config → Services → UserService → New user
   - Roles: that read-only role **only**, Admin unchecked
   - Authentication Scheme Name: `HTTPBasicScheme`
   - **Auto Logoff Enabled: false** — it defaults to 15 minutes, the same as the poll interval, which causes intermittent failures that are miserable to diagnose

### Fill in capacity and collection interval

Without these the roll-horizon guard cannot protect a point, and it will report `roll_horizon_unknown`. **Unknown is never treated as safe.**

Read them from Workbench → History Ext Manager, then:

```sql
UPDATE bas.point
   SET capacity = 500, collection_interval_s = 300, full_policy = 'roll'
 WHERE niagara_history_name = 'YourPointName';
```

To see what still needs doing:

```sql
SELECT point_name, roll_risk FROM bas.v_collection_health
WHERE roll_risk = 'roll_horizon_unknown';
```

### Classify a point

```sql
UPDATE bas.point SET point_role = 'supply_air_temp'
WHERE niagara_history_name = 'AHU$2d1_SupplyAirTemp';
```

Valid roles: `SELECT point_role, description FROM bas.point_role ORDER BY 1;`

**Don't guess.** An unclassified point is honest; a wrongly classified one makes every downstream answer confidently wrong.

### Change the schema

Never edit an applied migration or run ad-hoc `ALTER`. Add a new numbered file in `C:\dev\bas-db\migrations\` and run:

```powershell
cd C:\dev\bas-db
python scripts/migrate.py
```

The runner checksums applied files and refuses to continue if one has changed — that is deliberate, and it is what makes "what is the schema right now" answerable a year from now.

---

## Backups

**This matters more here than in most systems.** There is no source to re-import from.

```powershell
$d = Get-Date -Format "yyyy-MM-dd"
pg_dump "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -Fc -f "C:\dev\backups\bas_$d.dump"
```

Set that up as a scheduled task, daily. Keep backups **somewhere other than this machine** — a backup on the same disk as the database protects against very little.

Restore:

```powershell
pg_restore -d "postgresql://bas:bas_local_dev_only@localhost:5432/bas" --clean "C:\dev\backups\bas_2026-08-20.dump"
```

**Test a restore before you need one.** An untested backup is a guess.

---

## Health checks

**Daily, ten seconds:** open the Grafana Collection Health dashboard. If "Minutes since newest reading" is green, everything is fine.

**Weekly:**

```powershell
cd C:\dev\bas-collector
python -m collector status
```

Look for: `unsafe` points, non-zero gap counts, failed runs, and how many points remain unclassified.

**Or ask Claude** — *"is data still coming in?"* and *"is anything wrong with the building?"* cover both.

---

## Known limitations

**The lab JACE caps at 1,250 points and 26 devices.** That is two or three buildings. Beyond that you need a Niagara Supervisor — same role, server-class hardware, but a purchase and a server.

**Adding a building requires a terminal.** No UI exists for it. That is real debt, not an oversight — it was traded away deliberately for speed, and it means this is currently operable only by someone comfortable at a command line.

**It runs on one person's machine.** If the collector, database, and credentials all live on a laptop, this is one departure away from being unrecoverable. Moving it to a company-owned host is a milestone with a date, not something that happens eventually.

**TLS verification is disabled** (`NIAGARA_VERIFY_TLS=0`). The station's certificate fingerprint is logged on every discovery run — pin it, or install a proper certificate. On a flat building network, unverified TLS is a man-in-the-middle waiting to happen.

---

## Reference

| | |
|---|---|
| Station | `https://196.1.1.213` — `SpringGroveLabComputer`, JACE-9000, Niagara 4.15.4.24 |
| Station cert SHA-256 | `483bc6d6cbefa12914398e7e27010b6275b287503ac19e0c783482278dc186b4` |
| Niagara service account | `bas_collector`, role `pipelinereader`, scheme `HTTPBasicScheme` |
| Database | `postgresql://bas:...@localhost:5432/bas` |
| Read-only DB role | `bas_readonly` — used by Grafana and the MCP server |
| Collector | `C:\dev\bas-collector` |
| Database project | `C:\dev\bas-db` |
| MCP server | `C:\dev\bas-mcp` |
| Grafana | `http://localhost:3000` |

Each project has its own README with more detail. Project history and architecture decisions live in the Claude project under `claude/project-status.md`.
