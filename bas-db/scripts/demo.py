#!/usr/bin/env python3
"""
Load a week of synthetic building data with three deliberately planted faults,
then find them with SQL.

The point is not the fake data. The point is to demonstrate that the schema
actually supports the questions this project exists to answer, BEFORE any real
data arrives — and to show the shape of query the AI layer will eventually
generate.

Every fault below is a real, common, expensive HVAC failure that is invisible on
an alarm screen and obvious in trend data:

  1. Never reaching setpoint      — a unit calling for cooling it cannot deliver
  2. Commanded on, not running    — a fan that says it is on and is not
  3. Simultaneous heat and cool   — two systems fighting, burning energy twice

Note what the queries do NOT contain: point names. Every one is written against
point_role, so the same SQL works on any building whose points are classified.
That property is the entire reason the vocabulary exists.

Usage:
    python scripts/demo.py            load data and run the queries
    python scripts/demo.py --clean    remove the demo site and exit
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import psycopg
except ImportError:
    sys.exit("psycopg is not installed. Run:  pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SITE = "DEMO — Synthetic Building"
DAYS = 7
INTERVAL_S = 900  # 15 minutes


def banner(text: str) -> None:
    print(f"\n{'=' * 74}\n{text}\n{'=' * 74}")


def clean(conn: psycopg.Connection) -> None:
    """Remove the demo site. Children first — the foreign keys are there on purpose."""
    with conn.transaction():
        conn.execute(
            """
            DELETE FROM bas.point WHERE station_id IN (
                SELECT station_id FROM bas.station WHERE site_id IN (
                    SELECT site_id FROM bas.site WHERE name = %s))
            """, (SITE,))
        conn.execute(
            "DELETE FROM bas.station WHERE site_id IN "
            "(SELECT site_id FROM bas.site WHERE name = %s)", (SITE,))
        conn.execute(
            "DELETE FROM bas.equipment WHERE site_id IN "
            "(SELECT site_id FROM bas.site WHERE name = %s)", (SITE,))
        conn.execute("DELETE FROM bas.site WHERE name = %s", (SITE,))
        conn.execute("DELETE FROM bas.org WHERE name = 'DEMO Org'")


def seed(conn: psycopg.Connection) -> None:
    cur = conn.cursor()

    org_id = cur.execute(
        "INSERT INTO bas.org (name) VALUES ('DEMO Org') "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING org_id"
    ).fetchone()[0]
    site_id = cur.execute(
        "INSERT INTO bas.site (org_id, name, timezone) VALUES (%s,%s,'America/New_York') "
        "RETURNING site_id", (org_id, SITE)
    ).fetchone()[0]
    station_id = cur.execute(
        "INSERT INTO bas.station (site_id, niagara_station_name, niagara_version) "
        "VALUES (%s,'DemoStation','4.13.2.18') RETURNING station_id", (site_id,)
    ).fetchone()[0]

    equip = {}
    for name, etype in [("AHU-1", "ahu"), ("AHU-2", "ahu")]:
        equip[name] = cur.execute(
            "INSERT INTO bas.equipment (site_id, name, equip_type) VALUES (%s,%s,%s) "
            "RETURNING equipment_id", (site_id, name, etype)
        ).fetchone()[0]

    def add_point(eq: str, hist: str, role: str, unit: str | None, dtype: str) -> int:
        return cur.execute(
            """
            INSERT INTO bas.point
              (station_id, equipment_id, niagara_history_name, display_name, point_role,
               unit, data_type, collection_interval_s, capacity, full_policy)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,500,'roll')
            RETURNING point_id
            """,
            (station_id, equip[eq], f"{eq.replace('-', '$2d')}_{hist}",
             f"{eq} {hist}", role, unit, dtype, INTERVAL_S),
        ).fetchone()[0]

    p = {}
    for eq in ("AHU-1", "AHU-2"):
        p[(eq, "sat")]     = add_point(eq, "SupplyAirTemp",    "supply_air_temp",    "fahrenheit", "real")
        p[(eq, "sat_sp")]  = add_point(eq, "SupplyAirTempSp",  "supply_air_temp_sp", "fahrenheit", "real")
        p[(eq, "fan_cmd")] = add_point(eq, "SupplyFanCmd",     "supply_fan_cmd",     None,         "bool")
        p[(eq, "fan_st")]  = add_point(eq, "SupplyFanStatus",  "supply_fan_status",  None,         "bool")
        p[(eq, "clg")]     = add_point(eq, "CoolingValve",     "cooling_valve_cmd",  "percent",    "real")
        p[(eq, "htg")]     = add_point(eq, "HeatingValve",     "heating_valve_cmd",  "percent",    "real")

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=DAYS)
    steps = DAYS * 24 * 3600 // INTERVAL_S

    num_rows: list[tuple] = []
    bool_rows: list[tuple] = []

    for i in range(steps):
        ts = start + timedelta(seconds=i * INTERVAL_S)
        local_hour = (ts - timedelta(hours=4)).hour   # rough EDT, good enough for a demo
        occupied = 6 <= local_hour < 18
        day = i // (24 * 3600 // INTERVAL_S)

        for eq in ("AHU-1", "AHU-2"):
            setpoint = 55.0
            fan_cmd = occupied

            # --- Fault 1: AHU-1 cannot make setpoint on days 3-4 -------------
            starved = eq == "AHU-1" and day in (3, 4) and occupied
            sat = setpoint + (6.5 if starved else 0.0) \
                + 0.6 * math.sin(i / 8) + (0.0 if occupied else 4.0)

            # --- Fault 2: AHU-2's fan is commanded on but not running, day 5 --
            fan_status = fan_cmd
            if eq == "AHU-2" and day == 5 and occupied:
                fan_status = False

            # --- Fault 3: AHU-1 heats and cools at once, day 6 ---------------
            cooling = 65.0 if starved else (35.0 if occupied else 0.0)
            heating = 0.0
            if eq == "AHU-1" and day == 6 and occupied:
                cooling, heating = 45.0, 40.0

            num_rows += [
                (p[(eq, "sat")], ts, round(sat, 2)),
                (p[(eq, "sat_sp")], ts, setpoint),
                (p[(eq, "clg")], ts, cooling),
                (p[(eq, "htg")], ts, heating),
            ]
            bool_rows += [
                (p[(eq, "fan_cmd")], ts, fan_cmd),
                (p[(eq, "fan_st")], ts, fan_status),
            ]

    cur.executemany(
        "INSERT INTO bas.reading (point_id, ts, value_num) VALUES (%s,%s,%s) "
        "ON CONFLICT DO NOTHING", num_rows)
    cur.executemany(
        "INSERT INTO bas.reading (point_id, ts, value_bool) VALUES (%s,%s,%s) "
        "ON CONFLICT DO NOTHING", bool_rows)

    for pid in p.values():
        cur.execute(
            "INSERT INTO bas.sync_checkpoint (point_id, last_record_ts, last_run_at, last_status) "
            "VALUES (%s, %s, now(), 'ok') ON CONFLICT (point_id) DO UPDATE "
            "SET last_record_ts = EXCLUDED.last_record_ts",
            (pid, start + timedelta(seconds=steps * INTERVAL_S)))

    print(f"  Loaded {len(num_rows) + len(bool_rows):,} readings "
          f"across {len(p)} points and {DAYS} days.")


# -----------------------------------------------------------------------------
# The questions
# -----------------------------------------------------------------------------

Q_SETPOINT = """
SELECT
    sp.equipment_name,
    date_trunc('day', m.ts_local)                     AS day,
    round(avg(m.value_num)::numeric, 1)               AS avg_measured,
    round(avg(s.value_num)::numeric, 1)               AS avg_setpoint,
    round(avg(m.value_num - s.value_num)::numeric, 1) AS avg_deviation,
    count(*) FILTER (WHERE m.value_num > s.value_num + 2) AS intervals_over
FROM bas.v_setpoint_pair sp
JOIN bas.v_reading m ON m.point_id = sp.measured_point_id
JOIN bas.v_reading s ON s.point_id = sp.setpoint_point_id AND s.ts = m.ts
WHERE sp.measured_role = 'supply_air_temp'
  AND m.ts > now() - interval '8 days'
  AND m.local_hour BETWEEN 6 AND 17
GROUP BY 1, 2
HAVING count(*) FILTER (WHERE m.value_num > s.value_num + 2) > 0
ORDER BY avg_deviation DESC;
"""

Q_FAN = """
SELECT
    cs.equipment_name,
    date_trunc('day', c.ts_local) AS day,
    count(*)                      AS intervals_commanded_on_but_off,
    min(c.ts_local)               AS first_seen,
    max(c.ts_local)               AS last_seen
FROM bas.v_command_status_pair cs
JOIN bas.v_reading c ON c.point_id = cs.command_point_id
JOIN bas.v_reading s ON s.point_id = cs.status_point_id AND s.ts = c.ts
WHERE c.value_bool IS TRUE
  AND s.value_bool IS FALSE
  AND c.ts > now() - interval '8 days'
GROUP BY 1, 2
ORDER BY 3 DESC;
"""

Q_FIGHTING = """
SELECT
    clg.equipment_name,
    date_trunc('day', clg.ts_local) AS day,
    count(*)                        AS intervals_fighting,
    round(avg(clg.value_num)::numeric, 0) AS avg_cooling_pct,
    round(avg(htg.value_num)::numeric, 0) AS avg_heating_pct
FROM bas.v_reading clg
JOIN bas.v_reading htg
  ON  htg.equipment_id = clg.equipment_id
  AND htg.ts           = clg.ts
  AND htg.point_role   = 'heating_valve_cmd'
WHERE clg.point_role = 'cooling_valve_cmd'
  AND clg.value_num > 5
  AND htg.value_num > 5
  AND clg.ts > now() - interval '8 days'
GROUP BY 1, 2
ORDER BY 3 DESC;
"""

Q_COVERAGE = """
SELECT point_name, point_role, unit, roll_risk, seconds_since_last_record
FROM bas.v_collection_health
WHERE site_name = %s
ORDER BY point_name
LIMIT 8;
"""


def show(conn: psycopg.Connection, title: str, question: str, sql: str, params=()) -> None:
    banner(title)
    print(f'Question: "{question}"\n')
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("  (no rows — nothing detected)")
        return
    cols = [d.name for d in conn.execute(sql, params).description]
    widths = [max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="remove the demo data and exit")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env.")

    with psycopg.connect(url, autocommit=True) as conn:
        clean(conn)
        if args.clean:
            print("\nDemo data removed.\n")
            return 0

        banner("Loading synthetic data")
        with conn.transaction():
            seed(conn)

        show(conn, "FAULT 1 — never reaching setpoint",
             "Which air handlers failed to hit their supply air temperature setpoint, and when?",
             Q_SETPOINT)

        show(conn, "FAULT 2 — commanded on, not running",
             "Is anything commanded to run that is not actually running?",
             Q_FAN)

        show(conn, "FAULT 3 — simultaneous heating and cooling",
             "Is any unit heating and cooling at the same time?",
             Q_FIGHTING)

        show(conn, "Data coverage",
             "Do we actually have current data, and are we at risk of losing any?",
             Q_COVERAGE, (SITE,))

        banner("What to notice")
        print("""
  Not one of those queries mentions a point name. Every one is written against
  point_role, so the identical SQL runs against any building whose points are
  classified — regardless of what that building's integrator named things.

  That is the whole argument for the vocabulary, and it is why classifying
  points is not administrative tidying but the thing that makes the questions
  answerable at all.

  These are also the queries an AI layer would generate. It does not read
  trend rows and reason about temperatures; it picks the right query, the
  database does the arithmetic, and the model explains the result.

  Run  python scripts/demo.py --clean  to remove this data.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
