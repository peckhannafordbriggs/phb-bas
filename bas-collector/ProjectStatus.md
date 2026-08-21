# BAS Pipeline — Current State

**Updated:** 20 August 2026 (end of session 4)
**Scope:** PHB Platform is out of scope for now. Pipeline, database, and analysis standalone.

**Goal:** get building automation data off the JACEs into storage where an AI can analyse it and answer natural-language questions about the building.

---

## STATUS: complete and working, waiting on real building data

```
JACE ──oBIX──> collector ──> PostgreSQL ──┬──> Claude Desktop  (ask questions)
 ✓              ✓             ✓           ├──> Grafana         (look at trends)
                                          └──> fault rules
```

Every layer is built, tested, and running on Mahi's machine. **The only remaining dependency is external:** access to a production JACE so there's real building data instead of four synthetic lab points.

| Project | Location | Verification |
|---|---|---|
| `bas-db` | `C:\dev\bas-db` | **34/34 checks pass**, 5 migrations |
| `bas-collector` | `C:\dev\bas-collector` | **29/29 chain checks** + verified against real hardware |
| `bas-mcp` | `C:\dev\bas-mcp` | **21/21 checks**, connected to Claude Desktop |
| `bas-grafana` | `C:\dev\bas-grafana` | 2 dashboards, 19 queries validated |
| `niagara-probe` | `C:\dev\niagara-probe` | Superseded — `collector check` does the same job |

**Environment:** Postgres 17 native on Windows · Python 3.12 · Node 24 · Grafana 13 OSS · Claude Desktop with `mcp` 2.0 · everything under `C:\dev\` (deliberately out of OneDrive, whose Files On-Demand reparse points cause locking failures in dev folders).

### Collection runs itself now

Two Windows scheduled tasks, both verified returning `LastTaskResult = 0`:

| Task | Cadence |
|---|---|
| `BAS Collector Sync` | every 15 min, and at startup |
| `BAS Health Check` | every 30 min |

Hidden, survives reboot and logoff (S4U, no stored password). **No PowerShell window needs to stay open.** Installed via `Install-BasTasks.ps1`, removed with `-Uninstall`.

**Why scheduled runs rather than the `collector run` loop:** a long-lived process dies eventually — a network hiccup, an unhandled edge case at 3am — and stays dead until a human notices. With a 42-hour roll horizon that silence costs real data. Each scheduled `sync` is independent and short; if one fails the next catches up, because the checkpoint only advances on committed data.

**Alerting** (`healthcheck.py` + `Invoke-BasHealthCheck.ps1`) separates two things that matter differently:

- *Collection is late* → WARNING. Self-heals.
- *Points past half their roll horizon* → CRITICAL. The station is about to overwrite data permanently.

Alerts reach you three ways: a desktop notification (verified firing), a **Windows Event Log** entry under source `BAS Collector`, and `logs\healthcheck.log`. Email is optional via `BAS_ALERT_TO` / `BAS_SMTP_SERVER` — left off because M365 usually blocks basic SMTP auth and a half-working mail path is worse than none.

The event log matters more than it looks: a toast nobody is present to see is useless on a Sunday, but the event log still has the timestamp on Monday.

**Two gotchas fixed during setup, both worth remembering:**

1. **PowerShell 5.1 reads `.ps1` as ANSI unless there is a BOM.** Em-dashes in comments became garbage bytes, one of which the parser read as a quote — producing "string is missing the terminator" pointing at a line that was fine. All `.ps1` files here are pure ASCII with a BOM, verified programmatically.
2. **Task Scheduler surfaces the exit code as `LastTaskResult`.** The health check originally exited 1 on a warning, so the task list would show the health check as *failed* the moment it did its job — training you to ignore red on the one task whose silent failure means data loss. It now exits 0 whenever the check *ran*; non-zero is reserved for the check being unable to run at all.

**The runbook is `C:\dev\bas-db\RUNBOOK.md`** — symptom/cause/fix for every failure mode, how to add a building, how to recover. Written for someone who has never seen the code.

---

## Sessions 3–4 headline: oBIX is live, and real data is flowing

`https://196.1.1.213/obix/` serves the lobby. The riskiest assumption in the project resolved in our favour.

Getting there took four steps, none predictable from documentation:

1. `/obix/` returned **404**
2. Dragging `ObixNetwork` into the station failed: *"missing modules required: obixDriver-rt"*. **The module split is the trap** — `-wb` lives on the engineer's PC, `-rt` must be on the station host. A palette appearing proves nothing about the host.
3. Installing `-rt` required **commissioning** = a full kernel / Ubuntu Core / JVM / Niagara-core firmware upgrade, 4.15.2.38 → 4.15.4.24. Backed up via Station Copier first. Clean, ~20 min, several reboots.
4. After reboot, `ObixNetwork` dropped in and the servlet came up immediately.

**Now verified (previously UNVERIFIED): the oBIX servlet does not serve until an ObixNetwork component exists in the station.** Community guidance was right; Tridium's docs are silent.

**The correctness anchor passed.** Records in `bas.reading` were compared against the same history in Workbench over the same window — timestamps and values match exactly.

---

## The lab station — verified facts

| | |
|---|---|
| Host | `196.1.1.213`, **JACE-9000**, host ID `ATLAS-SD-2485-636C-C0C9-1AE9` |
| Station name | **`SpringGroveLabComputer`** — case-sensitive, appears literally in oBIX URLs |
| `/obix/about` | serverName `atlashost`, Niagara **4.15.4.24** |
| Brand | License **Vykon**; Workbench **iSMA CONTROLLI**. License accepts any brand |
| License owner | `Building Controls & Solutions`, project `Columbus Temperature Controls` — **not PH+B** |
| **SMA** | **Current to 2027-04-21** |
| Capacity ceiling | **1,250 points, 26 devices** |
| TLS cert SHA-256 | `483bc6d6cbefa12914398e7e27010b6275b287503ac19e0c783482278dc186b4` — pin rather than disabling verification |

**Licensed:** `obixDriver` never expires, no limits, **`export="true"`** (that attribute is what permits *serving* oBIX). Also `mqtt` and `opcUaServer` with no limits — real alternative extraction paths. `analytics` is licensed but **capped at 25 points**, a demo tier that reinforces building this externally. No `cloudLink`, no JSON Toolkit.

### Data and roll horizon

Four active points (`Temp1`–`Temp3` from the History Emulator, `points_RoomT` real with `fahrenheit`). Three system logs deactivated, not deleted.

**All four: 500 records at 300-second intervals → 41.7 hours of history.** Exactly Niagara's documented default, confirmed empirically. Poll interval 900s with a 4× margin requirement = 3,600s needed against 150,000s available. `collector status` reports **4 safe, 0 unknown, 0 unsafe**. The gating item from day one is closed.

### Authentication — tested, not assumed

| Approach | Result |
|---|---|
| Normal user login over HTTP | **Fails** — Niagara defaults to digest, which HTTP clients cannot satisfy |
| Browser session cookie | **Fails** — redirects to `/login` even with the browser's exact User-Agent |
| Dedicated `HTTPBasicScheme` account | **The only path that works** |

Created: `HTTPBasicScheme` added to `AuthenticationService` from the `baja` palette (absent by default) · role **`pipelinereader`**, read-only · user **`bas_collector`**, that role only, never expires, **Auto Logoff disabled** — it defaulted to 15 minutes, exactly the poll interval, which would have produced intermittent failures that are miserable to diagnose.

---

## THE ARCHITECTURE DECISION — central collector station

Commissioning a JACE means a full firmware upgrade. Twenty minutes on a lab box; a scheduled maintenance event with sign-off on a live controller. **Doing that per building does not scale.**

Instead: **one station as central collector**, other JACEs importing histories to it over the NiagaraNetwork.

| Direct to each JACE | Via a central station |
|---|---|
| Firmware upgrade **per building** | Firmware work **once**, already done |
| Maintenance window per building | No production downtime at all |
| Module install per JACE | Nothing installed on production JACEs |
| Bounded by each JACE's ~42h buffer | Central station keeps its own larger copy |

Adding a building becomes: a read-only user, a NiagaraNetwork connection, one config entry.

**Hard ceiling:** the lab JACE caps at 1,250 points / 26 devices — realistically **two or three buildings**. Beyond that needs a proper **Niagara Supervisor** (a purchase and a server). Nothing built now is wasted: to the collector a Supervisor is just a station serving oBIX. Change `bas.station.base_url`, re-run `discover`.

**Switching a station's route later is one field.** Imported histories land under the *source* station's name, so the same `(station, history_name)` identity works via either route — same `point_id`, history continues, no split.

`[UNVERIFIED]` That behaviour is HIGH confidence but unobserved. **Check the moment imports are on:** `https://196.1.1.213/obix/histories/` should show a *second* station folder. If everything flattens under `SpringGroveLabComputer`, a mapping is needed — much better caught then than after a year of data.

**Two operational consequences.** Stop calling it the lab station once it carries real data — it becomes infrastructure needing backups and an owner. And **adding a building currently requires a terminal**; making it clickable is real work, essentially the platform module we set aside.

---

## The analysis layer

**Claude Desktop** connects to the database through `bas-mcp` — eight tools: point inventory, trend retrieval, statistical summaries, fault rules, collection health, schema, and a guarded SQL escape hatch. Config lives at `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`.

**Two design rules it enforces:**

*Claude never does arithmetic on trend rows.* Ask for a month and the tool buckets it and says so. An LLM given thousands of numbers produces a plausible wrong average with nothing signalling it went astray. The database computes; Claude explains.

*Read-only, three independent layers* — a Postgres role with no write permission, read-only transactions, and a validator rejecting anything that isn't a SELECT. More paranoia than usual, for a specific reason: the JACE overwrites its own history every 42 hours, so once data lands here **it is the only copy in existence**. A bad DELETE cannot be restored from source.

**Grafana** (localhost:3000) reads the same database as `bas_readonly`. Two dashboards — Point Explorer and Collection Health — both with a **Building** filter, so one dashboard set serves every building rather than a copy per site.

---

## Design guarantees proven, not claimed

**Database (34):** idempotent re-insert creates no duplicates · genuine duplicates rejected · a reading cannot carry two typed values · a null *record* is distinct from no record · a rename creates a new `point_id` leaving old history intact · UTC renders correctly across a DST boundary · setpoints auto-pair with measurements via `point_role` · unit mismatch flagged · unknown roll horizon reports as *unknown*, never as safe.

**Collector (29):** units and datatypes captured at ingest from `#RecordDef` · `$`-escaped names stored verbatim · re-fetching everything creates zero duplicates · the roll guard *refuses* unsafe collection · overwritten data recorded as a `roll_overwrite` gap · checkpoints never advance on failure · a recovered point stops reporting errors · writes refused before a socket opens.

**MCP (21):** DELETE / UPDATE / DROP / INSERT / TRUNCATE / stacked statements / SET all refused · the role itself refuses writes independently of the validator · large windows aggregate rather than dumping rows · fault rules fire on planted faults.

### Bugs the testing caught

1. **Permanent false alarms.** A point that failed once then recovered reported `last_status='error'` forever, because the checkpoint was only touched when there was data to write — and "nothing new" is healthy. Fixed with `mark_success`.
2. **A non-deterministic mock.** Timestamps generated relative to request time made idempotency *appear* broken. Now snapped to a fixed grid.
3. **`obix:units/null` stored as the string "null".** Worse than nothing: every "has units" check passes and `unit_mismatch` never fires. Found in real data.
4. **Stuck-sensor rule flagged setpoints.** A setpoint holding at 55 °F is working correctly. Restricted to measurements only.
5. **Stuck-sensor rule missed real failures.** Requiring exactly zero variance missed a sensor sitting at 64.5 with std dev 0.08. A std-dev threshold is unit-dependent and untunable across buildings. **Now uses distinct-value count**, which is unit-independent — a live sensor produces many distinct values, a dead one produces a handful. Grafana later confirmed the design: `Temp1` shows 246 distinct values across 285 readings, so it is *healthy*, and a std-dev rule would have wrongly condemned it.
6. **`ALTER DEFAULT PRIVILEGES` set for the wrong role.** Recreating a view drops its grants, and the defaults were attached to the superuser while migrations run as `bas` — so they would never have fired. The symptom would have been a Grafana panel going blank months later with a permissions error that looks like a broken dashboard. Fixed with `FOR ROLE bas` and verified by creating a view and confirming `bas_readonly` can read it with no explicit grant.

---

## PENDING — production JACE access

Email sent asking: which JACE / building · its IP and whether `196.1.1.213` can reach it · a read-only user on it · **whether histories are configured on it at all**.

**That last one is the risk.** If nobody ever set up history extensions, this stops being a data project and becomes a Niagara engineering job first.

The email leads with what this does *not* require — no firmware, no module install, no restart, no downtime — because that is what turns it from a scary ask into a small one.

---

## Next steps

1. **Chase the email.** Everything else is built and waiting.
2. **Set up the database backup** — the one item from the "what's missing" audit still open. Currently holds ~2,000 rows of synthetic data and losing it would cost nothing. The day real history accumulates that stops being true permanently, and the JACE will not have a copy to re-import from. Needs a scheduled `pg_dump` to somewhere that is not the same disk.
3. **When access lands:** add the JACE under the central station's NiagaraNetwork, configure history imports, then immediately check `/obix/histories/` for a second station folder.
4. **Build point classification tooling** once real points exist. Bulk classify by name pattern, create equipment, link points. Deliberately not built yet — the right shape depends entirely on how that building's integrator named things, and guessing against four synthetic points would be wasted work. Most fault rules need `equipment_id`, and nothing currently sets it.
5. **Move the collector to an always-on host.** It is on a company laptop, which is better than a personal one, but laptops sleep and travel. Once real data is flowing, a collector down longer than 42 hours means the station overwrites uncollected records permanently. The gap gets recorded; the data does not come back.
6. **Pin the TLS fingerprint** (`483bc6d6...`) instead of running with `NIAGARA_VERIFY_TLS=0` indefinitely.
7. **Record the postgres superuser password** somewhere the company can reach. It exists only in Mahi's memory and is the recovery path for a database that will hold irreplaceable data.

## Open questions

- Which building does the production JACE serve, and whose asset is it?
- Are histories configured on it at all?
- Multi-vendor: PH+B is a mechanical contractor, so a portfolio likely includes Johnson Controls, Siemens, Trane, Automated Logic. If the scope becomes "whatever BAS a customer has," the adapter boundary stops being cheap insurance and becomes the core of the product.
- At what point does a Supervisor get purchased, and who owns that budget?
- `Temp1`–`Temp3` are History Emulator output and nobody knows what they represent. Left unclassified deliberately — inventing a role would make Claude answer confidently about something untrue.