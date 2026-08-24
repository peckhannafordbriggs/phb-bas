#!/usr/bin/env python3
"""
Prove the whole chain: mock station -> collector -> PostgreSQL.

Starts its own mock Niagara station, runs the collector against it, and asserts
the behaviours the design promises. Uses a dedicated test site and removes it
afterwards, so it is safe to run against a database with real data in it.

    python verify_chain.py

Every check corresponds to a promise. A promise that is not tested is a promise
that quietly stops being true.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from collector.config import Config
from collector.db import Repository
from collector.models import StationError, UnsafePollInterval
from collector.obix import ObixClient
from collector.sync import discover, sync

PORT = 8177
ORG = "VERIFY_ORG"
SITE = "VERIFY_SITE"

PASSED = 0
FAILED = 0


def section(name: str) -> None:
    print(f"\n{name}\n{'-' * len(name)}")


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {desc}")
    else:
        FAILED += 1
        print(f"  FAIL  {desc}")
        if detail:
            print(f"        {detail}")


def make_config(**overrides) -> Config:
    base = dict(
        base_url=f"http://127.0.0.1:{PORT}",
        username="test",
        password="test",
        cookie=None,
        verify_tls=False,
        timeout_s=15.0,
        database_url=os.environ["DATABASE_URL"],
        org_name=ORG,
        site_name=SITE,
        site_timezone="America/New_York",
        poll_interval_s=900,
        max_window_hours=24,
        max_records_per_request=1000,
        initial_backfill_days=30,
        enforce_roll_guard=True,
        roll_safety_factor=4,
    )
    base.update(overrides)
    return Config(**base)


def cleanup(dsn: str) -> None:
    """Remove the test site. Children before parents — the FKs are there on purpose."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            DELETE FROM public.bas_ingest_runs WHERE station_id IN (
                SELECT station_id FROM public.bas_stations WHERE site_id IN (
                    SELECT site_id FROM public.bas_sites WHERE name = %s))
            """, (SITE,))
        conn.execute(
            """
            DELETE FROM public.bas_points WHERE station_id IN (
                SELECT station_id FROM public.bas_stations WHERE site_id IN (
                    SELECT site_id FROM public.bas_sites WHERE name = %s))
            """, (SITE,))
        conn.execute(
            "DELETE FROM public.bas_stations WHERE site_id IN "
            "(SELECT site_id FROM public.bas_sites WHERE name = %s)", (SITE,))
        conn.execute("DELETE FROM public.bas_sites WHERE name = %s", (SITE,))
        conn.execute("DELETE FROM public.bas_orgs WHERE name = %s", (ORG,))


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env.")

    print(f"\nStarting mock Niagara station on port {PORT} ...")
    mock = subprocess.Popen(
        [sys.executable, "mock_station.py", "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    time.sleep(2.0)

    try:
        cleanup(dsn)
        run_checks(dsn)
    finally:
        mock.terminate()
        mock.wait(timeout=5)
        cleanup(dsn)

    print(f"\n{'=' * 62}")
    print(f"  {PASSED} passed, {FAILED} failed")
    print(f"{'=' * 62}\n")
    if FAILED:
        print("The collector does not behave as designed. Do not point it at a station.\n")
        return 1
    print("The chain works end to end. Ready for a real station.\n")
    return 0


def run_checks(dsn: str) -> None:
    cfg = make_config()
    repo = Repository(dsn)
    client = ObixClient(cfg.base_url, cfg.username, cfg.password,
                        verify_tls=False, timeout_s=15.0)
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def q(sql: str, params=()) -> list[dict]:
        return conn.execute(sql, params).fetchall()

    def one(sql: str, params=()):
        rows = q(sql, params)
        return list(rows[0].values())[0] if rows else None

    # -------------------------------------------------------------------
    section("Station identity")

    about = client.about()
    check("reads /obix/about without platform credentials",
          about.station_name == "LabStation", f"got {about.station_name}")
    check("reports the Niagara version (a discovery item, free)",
          about.product_version == "4.13.2.18", f"got {about.product_version}")

    # -------------------------------------------------------------------
    section("Discovery")

    summary = discover(client, repo, cfg)
    check(f"registered every history ({summary['created']} points)",
          summary["created"] == 8, f"got {summary['created']}")

    verbatim = one(
        "SELECT count(*) FROM public.bas_points WHERE niagara_history_name LIKE '%%$2d%%'")
    check("stores $-escaped names verbatim (needed for the URL)", verbatim >= 1)

    units = one("""
        SELECT count(*) FROM public.bas_points p JOIN public.bas_stations s USING (station_id)
        WHERE p.unit IS NOT NULL""")
    check(f"captured units at ingest from the #RecordDef prototype ({units} points)",
          units >= 5)

    types = q("""
        SELECT DISTINCT data_type FROM public.bas_points p JOIN public.bas_stations s USING (station_id)
        JOIN public.bas_sites si USING (site_id) WHERE si.name = %s""", (SITE,))
    kinds = {r["data_type"] for r in types}
    check("captured value datatypes, including bool", "bool" in kinds and "real" in kinds,
          f"got {kinds}")

    rediscover = discover(client, repo, cfg)
    check("rediscovery creates nothing new (idempotent)",
          rediscover["created"] == 0, f"created {rediscover['created']}")

    # Scope every sync below to this test's own station, so the results are not
    # polluted by any real points already in the database.
    station_id = one("""
        SELECT station_id FROM public.bas_stations st JOIN public.bas_sites s USING (site_id)
        WHERE s.name = %s LIMIT 1""", (SITE,))
    check("test station registered and isolatable", station_id is not None)

    # -------------------------------------------------------------------
    section("Collection")

    cfg_open = make_config(enforce_roll_guard=False)
    result = sync(client, repo, cfg_open, station_id=station_id)
    check(f"first pass wrote records ({result['written']:,})", result["written"] > 1000)
    check("all points succeeded", result["failed"] == 0, f"{result['failed']} failed")

    after_first = one("SELECT count(*) FROM public.bas_readings")

    nulls = one("""
        SELECT count(*) FROM public.bas_readings r JOIN public.bas_points p USING (point_id)
        JOIN public.bas_stations s USING (station_id) JOIN public.bas_sites si USING (site_id)
        WHERE si.name = %s AND r.value_num IS NULL AND r.value_bool IS NULL
          AND r.value_str IS NULL""", (SITE,))
    check(f"null RECORDS stored as rows, not dropped ({nulls} of them)", nulls > 0,
          "a null record means the sensor was down; dropping it would look like a gap")

    status_kept = one("""
        SELECT count(*) FROM public.bas_readings r JOIN public.bas_points p USING (point_id)
        JOIN public.bas_stations s USING (station_id) JOIN public.bas_sites si USING (site_id)
        WHERE si.name = %s AND r.status IS NOT NULL""", (SITE,))
    check("Niagara status flags preserved", status_kept > 0)

    tz_ok = one("""
        SELECT count(*) FROM public.bas_v_reading v JOIN public.bas_sites s ON s.name = v.site_name
        WHERE v.site_name = %s AND v.ts_local IS NOT NULL""", (SITE,))
    check("readings expose building-local time for occupancy analysis", tz_ok > 0)

    # -------------------------------------------------------------------
    section("Idempotency — the collector must be safe to re-run")

    incremental = sync(client, repo, cfg_open, station_id=station_id)
    check("second pass fetches only the tail (few requests)",
          incremental["requests"] < result["requests"] / 2,
          f"{incremental['requests']} vs {result['requests']}")

    scratch = sync(client, repo, cfg_open, station_id=station_id, from_scratch=True)
    after_refetch = one("SELECT count(*) FROM public.bas_readings")
    delta = after_refetch - after_first
    check(f"re-fetching EVERYTHING creates no duplicates (delta {delta})",
          delta <= 20, f"{delta} unexpected rows — only new real records should appear")
    check("full refetch reports ~zero writes", scratch["written"] <= 20,
          f"wrote {scratch['written']}")

    # -------------------------------------------------------------------
    section("The roll-horizon guard")

    conn.execute("""
        UPDATE public.bas_points SET capacity = 20, collection_interval_s = 60, full_policy = 'roll'
        WHERE station_id IN (SELECT station_id FROM public.bas_stations WHERE site_id IN
              (SELECT site_id FROM public.bas_sites WHERE name = %s))""", (SITE,))

    horizon = one("""
        SELECT roll_horizon_s FROM public.bas_points p JOIN public.bas_stations s USING (station_id)
        JOIN public.bas_sites si USING (site_id) WHERE si.name = %s LIMIT 1""", (SITE,))
    check("roll horizon computed from capacity x interval (20 x 60 = 1200)",
          horizon == 1200, f"got {horizon}")

    refused = False
    try:
        sync(client, repo, make_config(poll_interval_s=900), station_id=station_id)
    except UnsafePollInterval:
        refused = True
    check("REFUSES to collect when the poll interval risks silent data loss", refused,
          "this must fail loudly — the alternative is losing data with no error anywhere")

    ran = True
    try:
        sync(client, repo, make_config(poll_interval_s=60), station_id=station_id, only="OutsideAir")
    except UnsafePollInterval:
        ran = False
    check("allows collection once the interval is safe", ran)

    # -------------------------------------------------------------------
    section("Data loss is recorded, not hidden")

    conn.execute("""
        UPDATE public.bas_sync_checkpoints SET last_record_ts = now() - interval '10 days'
        WHERE point_id IN (SELECT point_id FROM public.bas_points p
             JOIN public.bas_stations s USING (station_id) JOIN public.bas_sites si USING (site_id)
             WHERE si.name = %s)""", (SITE,))

    before_gaps = one("SELECT count(*) FROM public.bas_data_gaps")
    sync(client, repo, cfg_open, station_id=station_id)
    after_gaps = one("SELECT count(*) FROM public.bas_data_gaps")
    check(f"records a data_gap when the station overwrote records first "
          f"({after_gaps - before_gaps} recorded)", after_gaps > before_gaps,
          "an unrecorded gap is indistinguishable from equipment being off")

    cause = one("SELECT cause FROM public.bas_data_gaps ORDER BY gap_id DESC LIMIT 1")
    check("gap is labelled roll_overwrite — the unrecoverable kind",
          cause == "roll_overwrite", f"got {cause}")

    # -------------------------------------------------------------------
    section("Failure and recovery")

    dead = ObixClient(f"http://127.0.0.1:{PORT + 1}", "test", "test",
                      verify_tls=False, timeout_s=3.0)
    before_cp = one("SELECT max(last_record_ts) FROM public.bas_sync_checkpoints")
    outcome = sync(dead, repo, cfg_open, station_id=station_id)
    after_cp = one("SELECT max(last_record_ts) FROM public.bas_sync_checkpoints")

    check("a totally unreachable station does not crash the run", outcome["failed"] > 0)
    check("checkpoints DO NOT advance when collection fails", before_cp == after_cp,
          f"{before_cp} -> {after_cp}")

    failures = one("SELECT max(consecutive_failures) FROM public.bas_sync_checkpoints")
    check("consecutive failures counted for alerting", failures >= 1, f"got {failures}")

    recovered = sync(client, repo, cfg_open, station_id=station_id)
    check("recovers automatically when the station returns", recovered["failed"] == 0)

    still_erroring = one("""
        SELECT count(*) FROM public.bas_sync_checkpoints c JOIN public.bas_points p USING (point_id)
        WHERE p.station_id = %s AND c.last_status <> 'ok'""", (station_id,))
    check("every checkpoint status returns to ok after recovery",
          still_erroring == 0,
          f"{still_erroring} point(s) still reporting error despite a successful pass — "
          f"this is what causes permanent false alarms in monitoring")

    # -------------------------------------------------------------------
    section("Read-only enforcement")

    blocked = False
    try:
        client._request("/obix/config/SomePoint/", method="PUT", body="<real val='99'/>")
    except StationError:
        blocked = True
    check("a write to the station is refused before a socket opens", blocked,
          "this is what stands between a bug here and someone's chiller")

    # -------------------------------------------------------------------
    section("Audit trail")

    runs = one("SELECT count(*) FROM public.bas_ingest_runs")
    check(f"every pass recorded in public.bas_ingest_runs ({runs} runs)", runs >= 5)

    has_errors = one("""
        SELECT count(*) FROM public.bas_ingest_runs
        WHERE status IN ('partial','failed') AND jsonb_array_length(errors) > 0""")
    check("failed runs record what actually went wrong", has_errors >= 1)

    conn.close()
    repo.close()


if __name__ == "__main__":
    sys.exit(main())
