# bas-grafana

Dashboards over the BAS database, for when you want to *look* at trends rather than ask about them.

Complements the MCP server rather than replacing it. Ask Claude when you have a question; open Grafana when you want to watch something.

---

## Setup

### 1. Install Grafana

```powershell
winget install --id Grafana.Grafana -e
```

It installs as a Windows service and starts automatically. Give it a minute, then open:

**http://localhost:3001**

> Port 3001, not Grafana's default 3000 - the platform's dev server is pinned to 3000.

Default login is `admin` / `admin`. It'll make you change the password on first sign-in.

### 2. Add the database as a data source

**Connections → Data sources → Add new data source → PostgreSQL**

| Field | Value |
|---|---|
| Host URL | `localhost:5432` |
| Database name | `phb_platform` |
| Username | `bas_readonly_platform` |
| Password | *(the one passed to `setup_readonly_role_platform.sql`)* |
| TLS/SSL Mode | `disable` |
| Version | leave at default |

Click **Save & test**. You want "Database Connection OK."

**Use `bas_readonly_platform`, not `postgres`.** That account cannot write to anything, so no dashboard query — including one you write by hand at 5pm on a Friday — can damage data that has no other copy in existence.

**It also cannot read the platform's own tables.** `phb_platform` holds `employees`, `audit_events`, `module_grants` and `draft_locks` alongside the building data. This role is granted `SELECT` table by table on `bas_*` only, so a dashboard cannot reach the employee directory. Create it with `C:\dev\bas-mcp\setup_readonly_role_platform.sql`.

> **Not `bas_readonly`, and not the `bas` database.** Both still exist — the standalone database is kept as a point-in-time snapshot — but the collector now writes to `phb_platform`. A datasource pointed at `bas` shows numbers frozen at the cutover and looks like a stalled collector.

### 3. Import the dashboards

**Dashboards → New → Import → Upload JSON file**

Do this twice:

- `dashboards/bas-point-explorer.json`
- `dashboards/bas-collection-health.json`

Each will ask you to pick a data source — choose the PostgreSQL one you just made.

---

## The dashboards

### Point Explorer

Pick a point from the dropdown, see what it's been doing. Latest value, average, range, and the trend.

Two panels are less obvious than they look:

**Distinct values** — colour-coded, and it's the stuck-sensor check. A live sensor sampling the physical world produces many distinct readings. Three distinct values over six hundred readings means it has stopped responding, and that signal works regardless of units or scale. Red means suspicious.

**Known data gaps** — periods we didn't collect. Worth looking at before concluding equipment was off. A gap means we weren't watching, which is a different thing entirely.

### Collection Health

Is data arriving, and are we losing any.

The number to watch is **Minutes since newest reading**. If it climbs well past your poll interval, the collector has stopped. Green under 30 minutes, red past 60.

**Records written per collector run** should be a steady low bar. A big spike usually means a backfill catching up after an outage — worth understanding rather than ignoring.

---

## If an import fails

The JSON was written against Grafana 11's schema and every query in it was executed against your actual database before shipping — but I couldn't test the import itself, so a panel may need nudging on a different Grafana version.

If that happens, don't debug JSON. Build the panel by hand — **Add panel → PostgreSQL → Code mode** — and paste the query you want:

**Trend for one point** *(time series panel)*

```sql
SELECT ts AS "time", value_num AS "value"
FROM bas_readings
WHERE point_id = $point AND $__timeFilter(ts)
ORDER BY ts;
```

**Every point's current status** *(table panel)*

```sql
SELECT point_name, point_role, unit, roll_risk, last_record_ts,
       round(seconds_since_last_record / 60.0) AS mins_ago
FROM bas_v_collection_health
WHERE is_active
ORDER BY seconds_since_last_record DESC NULLS FIRST;
```

**Is the collector alive** *(stat panel)*

```sql
SELECT EXTRACT(EPOCH FROM (now() - max(ts))) / 60 AS minutes_since_last_reading
FROM bas_readings;
```

**Compare a measurement across all equipment of a type** *(time series)*

```sql
SELECT ts AS "time", equipment_name AS metric, value_num AS "value"
FROM bas_v_reading
WHERE point_role = 'supply_air_temp' AND equip_type = 'ahu' AND $__timeFilter(ts)
ORDER BY ts;
```

That last one is what `point_role` buys you — one query, every AHU, any building, regardless of what the installer named things.

**Point selector variable** — Dashboard settings → Variables → New → Query:

```sql
SELECT point_name || COALESCE(' (' || point_role || ')', '') AS __text,
       point_id AS __value
FROM bas_v_point
WHERE is_active
ORDER BY site_name, point_name;
```

---

## Notes

**Grafana runs as a Windows service**, so it survives reboots and keeps working whether or not you're logged in. `Get-Service Grafana` to check.

**Everything is read-only**, so nothing here can hurt the data. Experiment freely.

**Anyone on the network can reach it** at `http://<your-machine>:3000` once the firewall allows it. Fine for showing someone a chart; worth thinking about before it holds anything sensitive.
