# bas-db

The database for the BAS pipeline: PostgreSQL schema, migrations, and a demo that proves it answers the questions the project exists for.

No collector yet, no Niagara code here. This is the destination — the shape the data has to land in for anything downstream to work.

---

## Setup

### 1. Do you have Docker?

Open PowerShell and run:

```powershell
docker --version
```

**If you get a version number**, you're set — continue to step 2.

**If it says the command isn't recognized**, install [Docker Desktop](https://www.docker.com/products/docker-desktop/), reboot when it asks, and make sure it's actually running (whale icon in the system tray) before continuing.

*Alternative if you'd rather not use Docker:* install [PostgreSQL 17](https://www.postgresql.org/download/windows/) directly, note the password you set for the `postgres` user, create a database called `bas`, and skip step 2. You'll need to edit `DATABASE_URL` in step 3 to match.

### 2. Start Postgres

```powershell
docker compose up -d
docker compose ps
```

The second command should show `bas-postgres` as `healthy` after a few seconds. If it says `starting`, wait and run it again.

### 3. Configure and install

```powershell
copy .env.example .env
pip install -r requirements.txt
```

The default `.env` matches what docker-compose starts, so you shouldn't need to edit it unless you went the native-install route.

### 4. Build the schema

```powershell
python scripts/migrate.py
```

You should see four migrations apply. Then confirm everything is actually true:

```powershell
python scripts/verify.py
```

**34 checks should pass.** If any fail, stop — the schema doesn't do what the design says and loading real data on top of it would bake the problem in.

### 5. See what it's for

```powershell
python scripts/demo.py
```

Loads a week of synthetic data for two air handlers with three deliberately planted faults, then finds them with SQL. Takes about ten seconds. `python scripts/demo.py --clean` removes it afterwards.

---

## What the demo actually demonstrates

It plants three faults that are real, common, expensive, and invisible on an alarm screen:

| Fault | Why it costs money |
|---|---|
| Never reaching setpoint | The unit is calling for cooling it can't deliver — a valve, a coil, or a chilled water problem, running at full effort and failing |
| Commanded on, not running | A fan the control system believes is running and isn't. Everything downstream is being controlled on a false premise |
| Simultaneous heating and cooling | Two systems fighting. You pay to heat air and pay again to cool it back down |

Then it finds all three. **Look at the SQL in `scripts/demo.py` — not one of those queries mentions a point name.** Every one is written against `point_role`, so the same SQL runs against any building whose points are classified, regardless of what that building's integrator called things.

That's the whole argument for the vocabulary, and it's why classifying points isn't administrative tidying — it's the thing that makes the questions answerable at all.

---

## The schema

```
org                     portfolio owner / customer
 └── site               a building  (holds the IANA timezone)
      ├── station       a JACE  (parent_station_id → optional Supervisor)
      │    └── point    one trended value  ← the crux table
      │         └── reading    (point_id, ts, value)
      └── equipment     AHU-3, VAV-204  (parent_equipment_id → served-by)

vocabularies:   point_role · equipment_type
operational:    sync_checkpoint · ingest_run · data_gap
relationships:  point_link
```

### Views — query these, not the tables

| View | For |
|---|---|
| `v_reading` | **The main one.** Every reading with equipment, site, role, unit, and site-local time on the row |
| `v_point` | Every point with full context — "what points exist", "which measure X" |
| `v_setpoint_pair` | Each measurement matched to the setpoint governing it, derived from roles |
| `v_command_status_pair` | Each command matched to its proof-of-operation status |
| `v_collection_health` | Is data flowing, and are we about to lose any |
| `v_data_dictionary` | The whole annotated schema in one query — paste into an LLM prompt as context |

The tables stay normalized because that's what's correct and what the collector writes to. The views exist because a six-table join from a reading to a building name is exactly the shape an LLM gets wrong — not with an error, but with a plausible wrong number.

---

## Decisions worth knowing

**Surrogate `point_id`, never a name.** Niagara point names change. When one does, the collector sees a new history and creates a new point row rather than silently rewriting the meaning of existing history. Verified: `verify.py` proves a rename leaves the original readings untouched.

**Everything is `timestamptz`, stored UTC.** `v_reading` exposes `ts_local`, `local_hour`, and `local_dow` derived from the site's IANA timezone, because "overnight" and "business hours" only mean anything in local time. Verified across a DST boundary: the same UTC hour renders as 12:00 in January and 13:00 in July.

**`bas.reading` is deliberately narrow.** No names, no units, no equipment. Denormalizing those multiplies storage roughly 5× and turns a rename into a billion-row rewrite. Join to `bas.point`.

**Primary key `(point_id, ts)`.** This is the idempotency guarantee, the dedup mechanism, and the main index in one. Re-running a collection cannot double-insert — it's structurally impossible, not a matter of the collector being careful.

**A null record is not a missing record.** A row with all three value columns NULL means the station reported a null — a sensor fault or a real gap. No row at all means we never collected it. Different things, and an AI that confuses them will report a shutdown that never happened. `bas.data_gap` records the second kind explicitly.

**`roll_horizon_s` is a generated column** (`capacity × collection_interval_s`). This is how far back a history reaches before Niagara overwrites it. The collector must poll far more often than this — polling slower loses data permanently, with no error and no gap marker anywhere. `v_collection_health` turns it into a `roll_risk` flag, and unknown capacity reports as *unknown*, never as safe.

**A Supervisor stays optional forever.** `station.parent_station_id` is a nullable self-reference. Introducing a Supervisor later needs no schema change at all.

**Everything is in the `bas` schema.** Costs nothing now; means this can later be attached alongside other schemas with zero chance of a table name colliding.

---

## Migrations

```powershell
python scripts/migrate.py            # apply pending
python scripts/migrate.py --status   # show state, change nothing
python scripts/migrate.py --reset    # drop the bas schema and start over
```

**Never edit a migration that has already been applied.** The runner checksums each file and refuses to continue if an applied one has changed, because at that point the database and the repo disagree and neither is obviously right. Fix forward with a new numbered file — `004_health_view_context.sql` is an example of exactly that.

This matters more than it sounds. "What is the schema right now" needs to be answerable a year from now without archaeology, and ad-hoc `ALTER` statements typed into a terminal are how a database becomes something nobody can reproduce.

---

## Example queries

```sql
-- Everything one point did yesterday, in building-local time
SELECT ts_local, value_num, unit
FROM bas.v_reading
WHERE point_name = 'AHU-1 SupplyAirTemp'
  AND ts > now() - interval '1 day'
ORDER BY ts;

-- Compare the same measurement across every unit of a type
SELECT equipment_name,
       round(avg(value_num)::numeric, 1) AS avg_f,
       round(min(value_num)::numeric, 1) AS min_f,
       round(max(value_num)::numeric, 1) AS max_f
FROM bas.v_reading
WHERE point_role = 'supply_air_temp'
  AND equip_type = 'ahu'
  AND ts > now() - interval '7 days'
GROUP BY equipment_name;

-- Anything running outside occupied hours
SELECT equipment_name, count(*) AS intervals
FROM bas.v_reading
WHERE point_role = 'supply_fan_status'
  AND value_bool IS TRUE
  AND (local_hour < 6 OR local_hour > 18)
  AND ts > now() - interval '7 days'
GROUP BY equipment_name
ORDER BY 2 DESC;

-- Points that need classifying — these are invisible to every generic query
SELECT point_name, niagara_history_name, unit
FROM bas.v_point
WHERE point_role IS NULL AND is_active;

-- Are we about to lose data we care about?
SELECT point_name, point_role, roll_risk, seconds_since_last_record
FROM bas.v_collection_health
WHERE roll_risk IN ('data_lost', 'at_risk', 'roll_horizon_unknown')
ORDER BY seconds_since_last_record DESC NULLS LAST;

-- The annotated schema, for feeding to an LLM as context
SELECT object_name, column_name, data_type, column_description
FROM bas.v_data_dictionary
ORDER BY object_name, column_name;
```

---

## Connecting a client

Any Postgres tool works. [DBeaver](https://dbeaver.io/) and [pgAdmin](https://www.pgadmin.org/) are both fine, and so is `psql`.

```
Host      localhost
Port      5432
Database  bas
User      bas
Password  bas_local_dev_only
```

---

## What's deliberately not here

**No collector.** That's next, and it's Python — it'll write to `bas.reading` and `bas.sync_checkpoint` using the exact idempotent-insert pattern `verify.py` proves works.

**No TimescaleDB.** At one building and roughly 17 million rows a year, plain Postgres doesn't notice. The primary key plus the BRIN index on `ts` carry it comfortably into the hundreds of millions. Adding Timescale later is an image change and a `CREATE EXTENSION`.

**No partitioning.** Same reasoning. When the readings table becomes uncomfortable, native declarative range partitioning by month is the next step, and it doesn't change any of these queries.

**No auth, no users, no permissions.** Deliberate. If this ever moves onto the PHB platform, the platform brings its own employee model — and there'll be nothing to reconcile because we never invented one.

---

## Next steps

1. **Fill in real capacity and collection interval** on `bas.point` from Workbench's History Ext Manager. Until those are populated, `roll_risk` reads `roll_horizon_unknown` and we genuinely cannot tell whether the building is losing data right now.
2. **Build the collector** — oBIX in, `bas.reading` out, checkpoint per point, roll-horizon guard.
3. **Classify points.** Assign `equipment_id` and `point_role`. Unclassified points are invisible to every query in this README, so treat the list from the "needs classifying" query above as a real backlog.
