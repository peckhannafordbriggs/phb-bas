#!/usr/bin/env python3
"""
Prove the schema's guarantees actually hold.

Every check here corresponds to a promise the design makes. A promise that is
not tested is a promise that quietly stops being true.

The whole run happens inside one transaction that is rolled back at the end, so
this leaves no data behind and is safe to run against a database that already
has real data in it.

Usage:
    python scripts/verify.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

try:
    import psycopg
except ImportError:
    sys.exit("psycopg is not installed. Run:  pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


PASSED = 0
FAILED = 0
_section = ""


def section(name: str) -> None:
    global _section
    _section = name
    print(f"\n{name}\n{'-' * len(name)}")


def check(description: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {description}")
    else:
        FAILED += 1
        print(f"  FAIL  {description}")
        if detail:
            print(f"        {detail}")


def expect_error(description: str, fn, error_type) -> None:
    """Assert that an operation is REJECTED. Constraints only count if they fire."""
    global PASSED, FAILED
    try:
        fn()
    except error_type:
        PASSED += 1
        print(f"  PASS  {description}")
        return
    except Exception as exc:  # noqa: BLE001
        FAILED += 1
        print(f"  FAIL  {description}")
        print(f"        wrong error type: {type(exc).__name__}: {exc}")
        return
    FAILED += 1
    print(f"  FAIL  {description}")
    print("        the operation was ALLOWED — the constraint is not protecting us")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env.")

    try:
        conn = psycopg.connect(url)
    except psycopg.OperationalError as exc:
        print(f"Could not connect.\n\n{exc}\n\nIs Postgres running? docker compose up -d")
        return 1

    try:
        run(conn)
    finally:
        conn.rollback()
        conn.close()

    print(f"\n{'=' * 60}")
    print(f"  {PASSED} passed, {FAILED} failed")
    print(f"{'=' * 60}\n")
    if FAILED:
        print("The schema does not do what the design says it does. Fix before ingesting.\n")
        return 1
    print("Every structural guarantee holds. Safe to start loading real data.\n")
    return 0


def run(conn: psycopg.Connection) -> None:
    cur = conn.cursor()

    # -------------------------------------------------------------------------
    section("Structure")

    expected_tables = {
        "org", "site", "station", "equipment", "point", "reading",
        "point_role", "equipment_type", "point_link",
        "sync_checkpoint", "ingest_run", "data_gap", "schema_migration",
    }
    found = {
        r[0]
        for r in cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='bas' AND table_type='BASE TABLE'"
        ).fetchall()
    }
    missing = expected_tables - found
    check("all expected tables exist", not missing, f"missing: {sorted(missing)}")

    expected_views = {
        "v_point", "v_reading", "v_setpoint_pair",
        "v_command_status_pair", "v_collection_health", "v_data_dictionary",
    }
    found_views = {
        r[0]
        for r in cur.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema='bas'"
        ).fetchall()
    }
    missing_views = expected_views - found_views
    check("all expected views exist", not missing_views, f"missing: {sorted(missing_views)}")

    # Everything after this point queries those objects. Bail with a clear message
    # rather than a stack trace if the schema is not actually in place.
    if missing or missing_views:
        print("\n  Schema is incomplete — run: python scripts/migrate.py")
        return

    documented = cur.execute(
        "SELECT count(*) FROM bas.v_data_dictionary WHERE column_description IS NOT NULL"
    ).fetchone()[0]
    check(
        f"schema is documented for LLM use ({documented} annotated columns)",
        documented >= 20,
    )

    # -------------------------------------------------------------------------
    section("Semantic vocabulary")

    roles = cur.execute("SELECT count(*) FROM bas.point_role").fetchone()[0]
    check(f"point roles seeded ({roles} roles)", roles >= 50)

    equip_types = cur.execute("SELECT count(*) FROM bas.equipment_type").fetchone()[0]
    check(f"equipment types seeded ({equip_types} types)", equip_types >= 15)

    sp_links = cur.execute(
        "SELECT count(*) FROM bas.point_role WHERE setpoint_for IS NOT NULL"
    ).fetchone()[0]
    check(f"setpoint->measurement links present ({sp_links})", sp_links >= 10)

    st_links = cur.execute(
        "SELECT count(*) FROM bas.point_role WHERE status_of IS NOT NULL"
    ).fetchone()[0]
    check(f"status->command links present ({st_links})", st_links >= 6)

    dangling = cur.execute(
        """
        SELECT count(*) FROM bas.point_role r
        WHERE (r.setpoint_for IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM bas.point_role t WHERE t.point_role = r.setpoint_for))
           OR (r.status_of IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM bas.point_role t WHERE t.point_role = r.status_of))
        """
    ).fetchone()[0]
    check("no role links point at nonexistent roles", dangling == 0)

    # -------------------------------------------------------------------------
    section("Fixture")

    org_id = cur.execute(
        "INSERT INTO bas.org (name) VALUES ('VERIFY_ORG') RETURNING org_id"
    ).fetchone()[0]
    site_id = cur.execute(
        "INSERT INTO bas.site (org_id, name, timezone) "
        "VALUES (%s, 'VERIFY_SITE', 'America/New_York') RETURNING site_id",
        (org_id,),
    ).fetchone()[0]
    station_id = cur.execute(
        "INSERT INTO bas.station (site_id, niagara_station_name) "
        "VALUES (%s, 'VerifyStation') RETURNING station_id",
        (site_id,),
    ).fetchone()[0]
    equip_id = cur.execute(
        "INSERT INTO bas.equipment (site_id, name, equip_type) "
        "VALUES (%s, 'AHU-VERIFY', 'ahu') RETURNING equipment_id",
        (site_id,),
    ).fetchone()[0]

    def make_point(hist_name: str, role: str | None, unit: str | None,
                   dtype: str = "real", interval: int | None = 900,
                   capacity: int | None = 500) -> int:
        return cur.execute(
            """
            INSERT INTO bas.point
              (station_id, equipment_id, niagara_history_name, display_name,
               point_role, unit, data_type, collection_interval_s, capacity, full_policy)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'roll')
            RETURNING point_id
            """,
            (station_id, equip_id, hist_name, hist_name.replace("$2d", "-"),
             role, unit, dtype, interval, capacity),
        ).fetchone()[0]

    sat = make_point("AHU$2d1_SupplyAirTemp", "supply_air_temp", "fahrenheit")
    sat_sp = make_point("AHU$2d1_SupplyAirTempSp", "supply_air_temp_sp", "fahrenheit")
    fan_cmd = make_point("AHU$2d1_FanCmd", "supply_fan_cmd", None, "bool")
    fan_stat = make_point("AHU$2d1_FanStatus", "supply_fan_status", None, "bool")

    check("fixture hierarchy created", all([org_id, site_id, station_id, equip_id, sat]))

    # -------------------------------------------------------------------------
    section("Roll horizon (generated column)")

    horizon = cur.execute(
        "SELECT roll_horizon_s FROM bas.point WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("roll_horizon_s = capacity x interval (500 x 900 = 450000)", horizon == 450_000,
          f"got {horizon}")

    cur.execute("UPDATE bas.point SET collection_interval_s=60 WHERE point_id=%s", (sat,))
    horizon2 = cur.execute(
        "SELECT roll_horizon_s FROM bas.point WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("roll_horizon_s recomputes when the interval changes (500 x 60 = 30000)",
          horizon2 == 30_000, f"got {horizon2}")
    cur.execute("UPDATE bas.point SET collection_interval_s=900 WHERE point_id=%s", (sat,))

    unknown = make_point("AHU$2d1_Unknown", None, None, "real", None, None)
    horizon3 = cur.execute(
        "SELECT roll_horizon_s FROM bas.point WHERE point_id=%s", (unknown,)
    ).fetchone()[0]
    check("roll_horizon_s is NULL when capacity/interval unknown", horizon3 is None)

    # -------------------------------------------------------------------------
    section("Idempotency — the collector must be safe to re-run")

    rows = [(sat, datetime(2026, 1, 15, 17, i * 15, tzinfo=timezone.utc), 55.0 + i)
            for i in range(4)]

    cur.executemany(
        "INSERT INTO bas.reading (point_id, ts, value_num) VALUES (%s,%s,%s) "
        "ON CONFLICT (point_id, ts) DO NOTHING",
        rows,
    )
    first = cur.execute(
        "SELECT count(*) FROM bas.reading WHERE point_id=%s", (sat,)
    ).fetchone()[0]

    cur.executemany(
        "INSERT INTO bas.reading (point_id, ts, value_num) VALUES (%s,%s,%s) "
        "ON CONFLICT (point_id, ts) DO NOTHING",
        rows,
    )
    second = cur.execute(
        "SELECT count(*) FROM bas.reading WHERE point_id=%s", (sat,)
    ).fetchone()[0]

    check(f"re-inserting identical records adds nothing ({first} then {second})",
          first == second == 4)

    def dup_without_conflict_clause():
        with conn.transaction():
            cur.execute(
                "INSERT INTO bas.reading (point_id, ts, value_num) VALUES (%s,%s,%s)",
                rows[0],
            )

    expect_error("a genuine duplicate is rejected, not silently accepted",
                 dup_without_conflict_clause, psycopg.errors.UniqueViolation)

    # -------------------------------------------------------------------------
    section("Value integrity")

    def two_values():
        with conn.transaction():
            cur.execute(
                "INSERT INTO bas.reading (point_id, ts, value_num, value_bool) "
                "VALUES (%s, '2026-03-01T00:00:00Z', 1.0, true)",
                (sat,),
            )

    expect_error("a reading cannot carry two typed values at once",
                 two_values, psycopg.errors.CheckViolation)

    cur.execute(
        "INSERT INTO bas.reading (point_id, ts, status) "
        "VALUES (%s, '2026-03-01T00:00:00Z', '{down}')",
        (sat,),
    )
    null_rec = cur.execute(
        "SELECT value_num IS NULL AND value_bool IS NULL AND value_str IS NULL "
        "FROM bas.reading WHERE point_id=%s AND ts='2026-03-01T00:00:00Z'",
        (sat,),
    ).fetchone()[0]
    check("a null RECORD is storable and distinct from no record at all", null_rec is True)

    def bad_type():
        with conn.transaction():
            cur.execute(
                "INSERT INTO bas.point (station_id, niagara_history_name, data_type) "
                "VALUES (%s, 'BadType', 'float64')",
                (station_id,),
            )

    expect_error("an undeclared data_type is rejected", bad_type, psycopg.errors.CheckViolation)

    # -------------------------------------------------------------------------
    section("Identity — a rename must not corrupt history")

    def duplicate_natural_key():
        with conn.transaction():
            cur.execute(
                "INSERT INTO bas.point (station_id, niagara_history_name) VALUES (%s,%s)",
                (station_id, "AHU$2d1_SupplyAirTemp"),
            )

    expect_error("the same history name cannot be registered twice on one station",
                 duplicate_natural_key, psycopg.errors.UniqueViolation)

    renamed = cur.execute(
        "INSERT INTO bas.point (station_id, equipment_id, niagara_history_name, point_role) "
        "VALUES (%s,%s,'AHU$2d1_SAT','supply_air_temp') RETURNING point_id",
        (station_id, equip_id),
    ).fetchone()[0]
    check("a renamed point becomes a NEW point_id, leaving old history intact",
          renamed != sat)

    still_there = cur.execute(
        "SELECT count(*) FROM bas.reading WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("the original point's readings are untouched by the rename", still_there == 5)
    cur.execute("DELETE FROM bas.point WHERE point_id=%s", (renamed,))

    # -------------------------------------------------------------------------
    section("Time — UTC storage, local-time analysis")

    cur.execute(
        "INSERT INTO bas.reading (point_id, ts, value_num) "
        "VALUES (%s, '2026-07-15T17:00:00Z', 70.0)",
        (sat,),
    )

    winter = cur.execute(
        "SELECT ts_local, local_hour FROM bas.v_reading "
        "WHERE point_id=%s AND ts='2026-01-15T17:00:00Z'",
        (sat,),
    ).fetchone()
    check("winter UTC 17:00 renders as 12:00 local (EST, UTC-5)",
          winter is not None and winter[1] == 12, f"got {winter}")

    summer = cur.execute(
        "SELECT ts_local, local_hour FROM bas.v_reading "
        "WHERE point_id=%s AND ts='2026-07-15T17:00:00Z'",
        (sat,),
    ).fetchone()
    check("summer UTC 17:00 renders as 13:00 local (EDT, UTC-4) — DST is handled",
          summer is not None and summer[1] == 13, f"got {summer}")

    ctx = cur.execute(
        "SELECT site_name, equipment_name, point_role, unit FROM bas.v_reading "
        "WHERE point_id=%s LIMIT 1",
        (sat,),
    ).fetchone()
    check("v_reading carries full context on every row",
          ctx == ("VERIFY_SITE", "AHU-VERIFY", "supply_air_temp", "fahrenheit"),
          f"got {ctx}")

    # -------------------------------------------------------------------------
    section("Semantic pairing — the point of the role vocabulary")

    pair = cur.execute(
        "SELECT measured_point_id, setpoint_point_id, unit_mismatch "
        "FROM bas.v_setpoint_pair WHERE equipment_id=%s",
        (equip_id,),
    ).fetchall()
    check("supply air temp is automatically paired with its setpoint",
          any(p[0] == sat and p[1] == sat_sp for p in pair), f"got {pair}")
    check("matching units are not flagged as a mismatch",
          all(p[2] is False for p in pair), f"got {pair}")

    cur.execute("UPDATE bas.point SET unit='celsius' WHERE point_id=%s", (sat_sp,))
    mismatch = cur.execute(
        "SELECT unit_mismatch FROM bas.v_setpoint_pair WHERE setpoint_point_id=%s",
        (sat_sp,),
    ).fetchone()[0]
    check("a degF measurement against a degC setpoint IS flagged", mismatch is True)
    cur.execute("UPDATE bas.point SET unit='fahrenheit' WHERE point_id=%s", (sat_sp,))

    cs = cur.execute(
        "SELECT command_point_id, status_point_id FROM bas.v_command_status_pair "
        "WHERE equipment_id=%s",
        (equip_id,),
    ).fetchall()
    check("fan command is automatically paired with its proof-of-running status",
          any(c[0] == fan_cmd and c[1] == fan_stat for c in cs), f"got {cs}")

    orphan = make_point("Orphan_SAT_SP", "supply_air_temp_sp", "fahrenheit")
    cur.execute("UPDATE bas.point SET equipment_id=NULL WHERE point_id=%s", (orphan,))
    orphan_pairs = cur.execute(
        "SELECT count(*) FROM bas.v_setpoint_pair WHERE setpoint_point_id=%s", (orphan,)
    ).fetchone()[0]
    check("a point with no equipment cannot pair (this is why equipment matters)",
          orphan_pairs == 0)

    # -------------------------------------------------------------------------
    section("Collection health")

    cur.execute(
        "INSERT INTO bas.sync_checkpoint (point_id, last_record_ts, last_status) "
        "VALUES (%s, now() - interval '10 minutes', 'ok')",
        (sat,),
    )
    risk = cur.execute(
        "SELECT roll_risk FROM bas.v_collection_health WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("a freshly collected point reads as ok", risk == "ok", f"got {risk}")

    cur.execute(
        "UPDATE bas.sync_checkpoint SET last_record_ts = now() - interval '10 days' "
        "WHERE point_id=%s",
        (sat,),
    )
    risk = cur.execute(
        "SELECT roll_risk FROM bas.v_collection_health WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("a point stale beyond its roll horizon reads as data_lost",
          risk == "data_lost", f"got {risk}")

    cur.execute(
        "UPDATE bas.sync_checkpoint SET last_record_ts = now() - interval '4 days' "
        "WHERE point_id=%s",
        (sat,),
    )
    risk = cur.execute(
        "SELECT roll_risk FROM bas.v_collection_health WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check("a point past half its roll horizon reads as at_risk",
          risk == "at_risk", f"got {risk}")

    risk = cur.execute(
        "SELECT roll_risk FROM bas.v_collection_health WHERE point_id=%s", (unknown,)
    ).fetchone()[0]
    check("unknown capacity reports as unknown, NOT as safe",
          risk in ("never_collected", "roll_horizon_unknown"), f"got {risk}")

    # -------------------------------------------------------------------------
    section("Cleanup behaviour")

    before = cur.execute(
        "SELECT count(*) FROM bas.reading WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    cur.execute("DELETE FROM bas.point WHERE point_id=%s", (sat,))
    after = cur.execute(
        "SELECT count(*) FROM bas.reading WHERE point_id=%s", (sat,)
    ).fetchone()[0]
    check(f"deleting a point cascades to its readings ({before} -> {after})",
          before > 0 and after == 0)

    def orphan_reading():
        with conn.transaction():
            cur.execute(
                "INSERT INTO bas.reading (point_id, ts, value_num) "
                "VALUES (999999999, now(), 1.0)"
            )

    expect_error("a reading cannot exist without a point",
                 orphan_reading, psycopg.errors.ForeignKeyViolation)


if __name__ == "__main__":
    sys.exit(main())
