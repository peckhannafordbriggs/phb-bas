#!/usr/bin/env python3
"""
Command line interface.

    python -m collector check       config, station reachability, schema
    python -m collector discover    enumerate histories and register them as points
    python -m collector sync        one collection pass
    python -m collector run         sync forever at POLL_INTERVAL_S
    python -m collector status      collection health
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from .config import Config
from .db import Repository
from .models import CollectorError, StationError, UnsafePollInterval
from .obix import ObixClient
from .sync import discover, roll_horizon_verdict, sync

log = logging.getLogger("collector")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def make_client(cfg: Config) -> ObixClient:
    return ObixClient(
        base_url=cfg.base_url,
        username=cfg.username,
        password=cfg.password,
        cookie=cfg.cookie,
        verify_tls=cfg.verify_tls,
        timeout_s=cfg.timeout_s,
    )


# =============================================================================

def cmd_check(cfg: Config) -> int:
    print()
    ok = True

    print("Configuration")
    print(f"  station        {cfg.base_url}")
    print(f"  auth           {cfg.auth_kind}")
    print(f"  TLS verify     {cfg.verify_tls}")
    print(f"  database       {cfg.database_url.split('@')[-1]}")
    print(f"  site           {cfg.org_name} / {cfg.site_name} ({cfg.site_timezone})")
    print(f"  poll interval  {cfg.poll_interval_s}s")
    print(f"  roll guard     {'enforced' if cfg.enforce_roll_guard else 'DISABLED'} "
          f"(x{cfg.roll_safety_factor} margin)")

    if cfg.auth_kind == "none":
        print("\n  WARNING: no credentials configured. Expect 401.")

    print("\nDatabase")
    try:
        repo = Repository(cfg.database_url)
        if not repo.schema_present():
            print("  FAIL  connected, but the bas_* tables are missing.")
            print("        This collector targets the platform database, where the tables")
            print("        live in public with a bas_ prefix and are managed by Prisma.")
            print("        Apply the phb-platform migrations first (npx prisma migrate deploy).")
            print("        If this database instead has a 'bas' schema with singular names,")
            print("        it is the old standalone database - see DATABASE_URL in .env.")
            ok = False
        else:
            c = repo.counts()
            print(f"  OK    connected — {c['active_points']} active points, "
                  f"{c['readings']:,} readings, {c['unclassified']} unclassified")
        repo.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {exc}")
        ok = False

    print("\nStation")
    try:
        client = make_client(cfg)
        about = client.about()
        print(f"  OK    {about.station_name or '(unnamed)'} — "
              f"{about.product_name or '?'} {about.product_version or ''}")
        print(f"        timezone {about.timezone or '(not reported)'}")
        fp = client.certificate_fingerprint()
        if fp:
            print(f"        cert SHA-256 {fp}")
        stations = client.list_stations()
        print(f"  OK    {len(stations)} station(s) in history space: {', '.join(stations)}")
    except StationError as exc:
        print(f"  FAIL  {exc}")
        if exc.diagnosis:
            print()
            for line in _wrap(exc.diagnosis, 72):
                print(f"        {line}")
        ok = False

    print()
    return 0 if ok else 1


def cmd_discover(cfg: Config) -> int:
    repo = Repository(cfg.database_url)
    client = make_client(cfg)
    try:
        summary = discover(client, repo, cfg)
    finally:
        repo.close()

    print()
    print(f"Registered {summary['created']} new point(s), refreshed {summary['updated']}.")
    if summary["deactivated"]:
        print(f"Marked {len(summary['deactivated'])} point(s) inactive (no longer reported).")
    print()
    print("Next: capacity and collection interval are NOT available over oBIX. Read them")
    print("from Workbench (History Ext Manager) and fill in public.bas_points.capacity and")
    print("public.bas_points.collection_interval_s. Until then the roll-horizon guard cannot")
    print("protect these points, and unknown is not the same as safe.")
    print()
    return 0


def cmd_sync(cfg: Config, from_scratch: bool, only: str | None) -> int:
    repo = Repository(cfg.database_url)
    client = make_client(cfg)
    try:
        result = sync(client, repo, cfg, from_scratch=from_scratch, only=only)
    except UnsafePollInterval as exc:
        print(f"\nREFUSED: {exc}\n")
        print(exc.diagnosis)
        print()
        return 2
    finally:
        repo.close()

    print()
    print(f"  run {result.get('run_id', '-')}: {result['written']:,} records written "
          f"from {result.get('requests', 0)} requests")
    print(f"  {result['succeeded']}/{result['points']} points ok, status {result['status']}")
    if result.get("gaps"):
        print(f"  {result['gaps']} point(s) had UNRECOVERABLE data loss — see public.bas_data_gaps")
    if result.get("unknown_horizon"):
        print(f"  {result['unknown_horizon']} point(s) have unknown roll horizon")
    print()
    return 0 if result["status"] != "failed" else 1


def cmd_run(cfg: Config) -> int:
    stop = False

    def handle(_sig, _frm):
        nonlocal stop
        stop = True
        log.info("Stopping after the current pass...")

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    repo = Repository(cfg.database_url)
    client = make_client(cfg)
    log.info("Collecting every %ds. Ctrl+C to stop.", cfg.poll_interval_s)

    try:
        while not stop:
            started = time.monotonic()
            try:
                result = sync(client, repo, cfg)
                log.info(
                    "pass complete: %d records, %d/%d points ok",
                    result["written"], result["succeeded"], result["points"],
                )
            except UnsafePollInterval as exc:
                log.error("REFUSED: %s", exc)
                log.error("%s", exc.diagnosis)
                return 2
            except CollectorError as exc:
                # A station being unreachable is expected and temporary. The
                # checkpoint has not moved, so the next pass simply catches up.
                log.error("pass failed: %s", exc)

            elapsed = time.monotonic() - started
            sleep_for = max(5.0, cfg.poll_interval_s - elapsed)
            for _ in range(int(sleep_for)):
                if stop:
                    break
                time.sleep(1)
    finally:
        repo.close()

    log.info("Stopped.")
    return 0


def cmd_status(cfg: Config) -> int:
    repo = Repository(cfg.database_url)
    try:
        counts = repo.counts()
        print()
        print(f"  active points  {counts['active_points']}")
        print(f"  readings       {counts['readings']:,}")
        print(f"  unclassified   {counts['unclassified']}  (invisible to role-based queries)")
        print(f"  recorded gaps  {counts['gaps']}")

        rows = repo.conn.execute(
            """
            SELECT roll_risk, count(*) AS n
            FROM public.bas_v_collection_health
            WHERE is_active GROUP BY 1 ORDER BY 2 DESC
            """
        ).fetchall()
        if rows:
            print("\n  collection risk")
            for r in rows:
                print(f"    {r['roll_risk']:<22} {r['n']}")

        points = repo.active_points()
        if points:
            cfg_verdicts = [roll_horizon_verdict(p, cfg)[0] for p in points]
            unsafe = cfg_verdicts.count("unsafe")
            unknown = cfg_verdicts.count("unknown")
            print(f"\n  at {cfg.poll_interval_s}s poll interval: "
                  f"{cfg_verdicts.count('ok')} safe, {unknown} unknown, {unsafe} unsafe")

        runs = repo.conn.execute(
            """
            SELECT run_id, started_at, status, points_succeeded, points_attempted,
                   records_written
            FROM public.bas_ingest_runs ORDER BY started_at DESC LIMIT 5
            """
        ).fetchall()
        if runs:
            print("\n  recent runs")
            for r in runs:
                print(f"    {r['started_at']:%Y-%m-%d %H:%M}  {r['status']:<8} "
                      f"{r['points_succeeded']}/{r['points_attempted']} points  "
                      f"{r['records_written']:,} records")
        print()
    finally:
        repo.close()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}" if line else w
    if line:
        lines.append(line)
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["check", "discover", "sync", "run", "status"])
    ap.add_argument("--from-scratch", action="store_true",
                    help="ignore checkpoints and re-fetch from the start (safe: idempotent)")
    ap.add_argument("--only", help="limit to points whose name contains this text")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)

    try:
        cfg = Config.from_env()
    except SystemExit as exc:
        print(f"\n{exc}\n")
        return 1

    try:
        if args.command == "check":
            return cmd_check(cfg)
        if args.command == "discover":
            return cmd_discover(cfg)
        if args.command == "sync":
            return cmd_sync(cfg, args.from_scratch, args.only)
        if args.command == "run":
            return cmd_run(cfg)
        if args.command == "status":
            return cmd_status(cfg)
    except CollectorError as exc:
        print(f"\nFAILED: {exc}\n")
        if exc.diagnosis:
            for line in _wrap(exc.diagnosis, 72):
                print(f"  {line}")
            print()
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.\n")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
