#!/usr/bin/env python3
"""
Is collection actually working, and are we about to lose data permanently?

Two different questions, and conflating them is the mistake this script exists
to avoid:

  * "Collection is late" is an inconvenience. The next run catches up.
  * "We are approaching the roll horizon" is imminent, permanent, unrecoverable
    data loss. The station overwrites its own history, so anything not collected
    before then exists nowhere, ever again.

The second one is the reason this runs at all. A collector that stops on a
Friday evening and is noticed on Monday has destroyed a weekend of building
history - silently, with no error anywhere in Niagara.

Exit codes, so a scheduler or monitoring system can act on them:
    0  OK
    1  WARNING  - degraded, catches up on its own
    2  CRITICAL - data is being lost, or is about to be

    python healthcheck.py           human-readable report
    python healthcheck.py --quiet   print only if something is wrong
    python healthcheck.py --json    machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    print("CRITICAL: psycopg not installed. Run: pip install -r requirements.txt")
    sys.exit(2)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


OK, WARNING, CRITICAL = 0, 1, 2
LABEL = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def check(dsn: str) -> tuple[int, list[dict]]:
    """Return (worst_status, findings)."""
    poll_s = _int("POLL_INTERVAL_S", 900)
    findings: list[dict] = []
    worst = OK

    def add(status: int, headline: str, detail: str) -> None:
        nonlocal worst
        worst = max(worst, status)
        findings.append({"status": LABEL[status], "headline": headline, "detail": detail})

    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True, connect_timeout=10)
    except psycopg.Error as exc:
        return CRITICAL, [{
            "status": "CRITICAL",
            "headline": "Cannot reach the database",
            "detail": f"{exc}\n\nCollection cannot be running, and nothing is being stored. "
                      f"Check the postgresql service is started.",
        }]

    with conn:
        # -- 1. Has the collector run at all recently? --------------------
        run = conn.execute("""
            SELECT started_at, finished_at, status, points_succeeded, points_attempted,
                   records_written,
                   EXTRACT(EPOCH FROM (now() - started_at))::bigint AS age_s
            FROM bas.ingest_run
            WHERE status IN ('ok','partial')
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()

        if run is None:
            add(CRITICAL, "The collector has never completed a run",
                "No successful entry in bas.ingest_run. Collection has never worked, or the "
                "audit table was cleared.")
        else:
            age = run["age_s"]
            mins = age // 60
            if age > poll_s * 4:
                add(CRITICAL, f"Collection stalled - last successful run was {mins} minutes ago",
                    f"Expected every {poll_s // 60} minutes. The collector is not running, cannot "
                    f"reach the station, or cannot write to the database. Check the "
                    f"'BAS Collector Sync' scheduled task and the log at logs\\collector.log.")
            elif age > poll_s * 2:
                add(WARNING, f"Collection is late - last successful run was {mins} minutes ago",
                    f"Expected every {poll_s // 60} minutes. One missed cycle is usually harmless; "
                    f"if this persists it becomes critical.")

        # -- 2. THE IMPORTANT ONE: are we about to lose data? -------------
        #
        # roll_risk already encodes this. 'data_lost' means the station has
        # already overwritten records we never collected - unrecoverable.
        # 'at_risk' means past half the roll horizon, so a missed cycle or two
        # tips it over.
        risk = conn.execute("""
            SELECT roll_risk, count(*) AS points,
                   min(point_name) AS example
            FROM bas.v_collection_health
            WHERE is_active GROUP BY 1
        """).fetchall()
        by_risk = {r["roll_risk"]: r for r in risk}

        if lost := by_risk.get("data_lost"):
            add(CRITICAL,
                f"DATA ALREADY LOST on {lost['points']} point(s)",
                f"The station overwrote records before we collected them. This is permanent - "
                f"Niagara keeps no other copy. e.g. {lost['example']}. "
                f"See bas.data_gap for exactly what is missing.")

        if at_risk := by_risk.get("at_risk"):
            add(CRITICAL,
                f"{at_risk['points']} point(s) are past half their roll horizon",
                f"Another missed cycle or two and the station starts overwriting data we have "
                f"not collected. e.g. {at_risk['example']}. Get collection running now.")

        if unknown := by_risk.get("roll_horizon_unknown"):
            add(WARNING,
                f"{unknown['points']} point(s) have unknown roll horizon",
                "capacity and collection_interval_s are not filled in from Workbench, so we "
                "cannot tell whether the poll cadence is safe for them. Unknown is not the "
                "same as safe.")

        if never := by_risk.get("never_collected"):
            add(WARNING, f"{never['points']} point(s) have never been collected",
                f"Registered by discover but never synced. e.g. {never['example']}")

        # -- 3. Points failing repeatedly ---------------------------------
        failing = conn.execute("""
            SELECT count(*) AS n, min(point_id) AS example_id,
                   max(consecutive_failures) AS worst, min(last_error) AS sample_error
            FROM bas.sync_checkpoint WHERE consecutive_failures >= 3
        """).fetchone()
        if failing and failing["n"]:
            add(WARNING,
                f"{failing['n']} point(s) failing repeatedly ({failing['worst']} consecutive)",
                f"Other points may be fine, so overall collection can look healthy. "
                f"Sample error: {(failing['sample_error'] or '')[:200]}")

        # -- 4. New gaps recorded ------------------------------------------
        gaps = conn.execute("""
            SELECT count(*) AS n, sum(EXTRACT(EPOCH FROM (gap_end - gap_start)))::bigint AS total_s
            FROM bas.data_gap WHERE detected_at > now() - interval '24 hours'
        """).fetchone()
        if gaps and gaps["n"]:
            hours = (gaps["total_s"] or 0) / 3600
            add(CRITICAL, f"{gaps['n']} new data gap(s) recorded in the last 24 hours",
                f"Roughly {hours:.1f} hours of building history was not collected and cannot be "
                f"recovered. Query bas.data_gap for details.")

    return worst, findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="print only when something is wrong")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("CRITICAL: DATABASE_URL is not set.")
        return CRITICAL

    status, findings = check(dsn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.json:
        print(json.dumps({"checked_at": now, "status": LABEL[status], "findings": findings},
                         indent=2))
        return status

    if args.quiet and status == OK:
        return OK

    print(f"\nBAS collection health: {LABEL[status]}   ({now})")
    print("=" * 68)
    if not findings:
        print("  Collection is running, nothing at risk.")
    for f in findings:
        print(f"\n  [{f['status']}] {f['headline']}")
        for line in _wrap(f["detail"], 64):
            print(f"      {line}")
    print()
    return status


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
