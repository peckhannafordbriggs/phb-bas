"""
Orchestration: discovery, the sync loop, and the roll-horizon guard.

Backfill and incremental collection are deliberately the SAME code path with
different starting points. There is no second system, because two systems means
two sets of bugs and one of them is always the one nobody tests.

Requests are issued sequentially, one point at a time. This is a building
controller running an occupied building, and it is serving a web UI and running
control logic while we talk to it. Five hundred sequential requests at ~200ms is
under two minutes, which is comfortably inside a 15-minute poll cycle. Adding
concurrency would buy time we do not need at a cost the JACE does pay.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import PointRow, Repository
from .models import (
    HistoryMeta,
    StationError,
    SyncOutcome,
    UnsafePollInterval,
)
from .obix import ObixClient

log = logging.getLogger("collector")


# =============================================================================
# The guard
# =============================================================================

def roll_horizon_verdict(point: PointRow, cfg: Config) -> tuple[str, str]:
    """
    Decide whether this point is safe to collect on the configured schedule.

    Returns (verdict, explanation) where verdict is one of:
        ok       — comfortable margin before the station overwrites
        unknown  — we cannot tell, because capacity/interval are not filled in
        unsafe   — the poll interval is too slow; data WILL be lost silently

    "unknown" is never treated as "ok". The whole reason this exists is that the
    failure it guards against produces no error, no log line, and no gap marker
    anywhere in Niagara — you find out months later looking at a chart with
    holes in it.
    """
    if point.roll_horizon_s is None:
        return (
            "unknown",
            f"capacity and/or collection interval are not recorded for "
            f"'{point.name}'. Read them from Workbench (History Ext Manager) or BQL "
            f"and fill in bas.point.capacity and bas.point.collection_interval_s. "
            f"Until then we cannot know whether this schedule loses data.",
        )

    required = cfg.poll_interval_s * cfg.roll_safety_factor
    if point.roll_horizon_s < required:
        return (
            "unsafe",
            f"'{point.name}' holds {point.roll_horizon_s}s of history "
            f"({point.capacity} records x {point.collection_interval_s}s), but the poll "
            f"interval is {cfg.poll_interval_s}s and we want at least "
            f"{cfg.roll_safety_factor}x margin ({required}s). One missed run and records "
            f"are overwritten permanently. Either poll faster or raise the history "
            f"capacity on the station.",
        )

    return ("ok", "")


# =============================================================================
# Discovery
# =============================================================================

def discover(client: ObixClient, repo: Repository, cfg: Config) -> dict:
    """
    Enumerate the station's histories and register them as points.

    Idempotent, and deliberately non-destructive: it will not overwrite a
    point_role, an equipment assignment, or capacity/interval values a human has
    filled in. Rediscovery must never undo classification work.
    """
    about = client.about()
    station_name = about.station_name

    log.info("Station: %s", station_name or "(not reported)")
    log.info("Product: %s %s", about.product_name or "?", about.product_version or "")
    log.info("Station timezone: %s", about.timezone or "(not reported)")

    fp = client.certificate_fingerprint()
    if fp:
        log.info("TLS certificate SHA-256: %s", fp)
        if not cfg.verify_tls:
            log.warning(
                "TLS verification is DISABLED. Pin the fingerprint above for production "
                "rather than leaving verification off."
            )

    site_id = repo.ensure_site(cfg.org_name, cfg.site_name, cfg.site_timezone)

    stations = client.list_stations()
    if not stations:
        raise StationError(
            "No stations found under /obix/histories/",
            "Either no histories are configured on this station, or the service account "
            "cannot see them. Check the History space in Workbench, and check the "
            "account's Category Service scoping.",
        )
    log.info("Stations in history space: %s", ", ".join(stations))

    summary = {"stations": {}, "created": 0, "updated": 0, "deactivated": []}

    for st_name in stations:
        station_id = repo.ensure_station(
            site_id, st_name, cfg.base_url, about.product_version
        )
        names = client.list_histories(st_name)
        log.info("  %s: %d histories", st_name, len(names))

        created = updated = 0
        for i, name in enumerate(names, 1):
            try:
                meta = client.history_meta(st_name, name)
                # Units and datatype only ever appear in a query response, never
                # on the history object. One tiny query per point, at discovery
                # time only.
                definition = client.probe_definition(st_name, name, meta.query_href)
                meta.unit = definition.unit
                meta.data_type = definition.data_type
                if not meta.timezone:
                    meta.timezone = definition.timezone

                _pid, was_created = repo.upsert_point(station_id, meta)
                created += was_created
                updated += not was_created

                if i % 25 == 0:
                    log.info("    ... %d/%d", i, len(names))
            except StationError as exc:
                log.warning("    could not read '%s': %s", name, exc)

        gone = repo.mark_missing_points_inactive(station_id, set(names))
        for g in gone:
            log.warning(
                "  '%s' is no longer reported by the station — marked inactive, not deleted. "
                "Usually a rename. Its history is still valid and still ours.", g
            )

        summary["stations"][st_name] = {"histories": len(names), "created": created}
        summary["created"] += created
        summary["updated"] += updated
        summary["deactivated"] += gone

    return summary


# =============================================================================
# Sync
# =============================================================================

def sync_point(
    client: ObixClient,
    repo: Repository,
    cfg: Config,
    point: PointRow,
    until: datetime,
    from_scratch: bool = False,
) -> SyncOutcome:
    """
    Bring one point up to date.

    The loop is bounded in three ways at once — a maximum window per request, a
    maximum record count per request, and a hard iteration cap — because the
    thing on the other end is a building controller, not a database.
    """
    outcome = SyncOutcome(point_id=point.point_id, point_name=point.name)

    since = None if from_scratch else point.last_record_ts
    if since is None:
        since = until - timedelta(days=cfg.initial_backfill_days)

    # If the station has already overwritten records past our checkpoint, that
    # data is gone. Record it rather than letting it become an unexplained hole.
    if point.roll_horizon_s and point.last_record_ts:
        earliest_still_held = until - timedelta(seconds=point.roll_horizon_s)
        if point.last_record_ts < earliest_still_held:
            repo.record_gap(
                point.point_id,
                point.last_record_ts,
                earliest_still_held,
                "roll_overwrite",
                f"Checkpoint was {point.last_record_ts.isoformat()}, but the station only "
                f"retains {point.roll_horizon_s}s. These records were overwritten before "
                f"we collected them and cannot be recovered.",
            )
            outcome.gap_recorded = (point.last_record_ts, earliest_still_held)
            log.error(
                "  %s: DATA LOST — station overwrote records between %s and %s",
                point.name,
                point.last_record_ts.isoformat(timespec="seconds"),
                earliest_still_held.isoformat(timespec="seconds"),
            )
            since = earliest_still_held

    window = timedelta(hours=cfg.max_window_hours)
    max_iterations = 200  # backstop against a station that never advances
    iterations = 0

    try:
        while since < until and iterations < max_iterations:
            iterations += 1
            window_end = min(since + window, until)

            result = client.query(
                point.niagara_station_name,
                point.niagara_history_name,
                since,
                window_end,
                limit=cfg.max_records_per_request,
                query_href=None,
            )
            outcome.requests_made += 1

            records = [r for r in result.records if r.ts > since] if point.last_record_ts else result.records

            if not records:
                since = window_end
                continue

            newest = max(r.ts for r in records)
            written = repo.write_batch(point.point_id, records, newest)
            outcome.records_written += written
            outcome.new_checkpoint = newest

            # If we filled the limit we have not covered the whole window, so
            # continue from the newest record rather than jumping to the end.
            if len(result.records) >= cfg.max_records_per_request:
                if newest <= since:
                    log.warning(
                        "  %s: station returned a full page that did not advance past %s. "
                        "Stopping this point to avoid looping.", point.name, since.isoformat()
                    )
                    break
                since = newest
            else:
                since = window_end

        if iterations >= max_iterations:
            log.warning(
                "  %s: hit the %d-request cap for one run. Not an error — a large backfill "
                "simply continues on the next run.", point.name, max_iterations
            )

        # A pass that found nothing is still a successful pass. Mark it, or a
        # point that fails once and then recovers reports 'error' forever.
        if outcome.records_written == 0:
            repo.mark_success(point.point_id)

    except StationError as exc:
        outcome.error = str(exc)
        repo.record_failure(point.point_id, str(exc))
        log.error("  %s: %s", point.name, exc)

    return outcome


def sync(
    client: ObixClient,
    repo: Repository,
    cfg: Config,
    station_id: int | None = None,
    from_scratch: bool = False,
    only: str | None = None,
) -> dict:
    """Run one full collection pass across every active point."""
    until = datetime.now(timezone.utc)
    points = repo.active_points(station_id)

    if only:
        points = [p for p in points if only.lower() in p.name.lower()
                  or only.lower() in p.niagara_history_name.lower()]

    if not points:
        log.warning("No active points. Run `discover` first.")
        return {"points": 0, "written": 0}

    # --- the guard, before any network traffic ---------------------------
    verdicts = {p.point_id: roll_horizon_verdict(p, cfg) for p in points}
    unsafe = [p for p in points if verdicts[p.point_id][0] == "unsafe"]
    unknown = [p for p in points if verdicts[p.point_id][0] == "unknown"]

    if unsafe and cfg.enforce_roll_guard:
        detail = "\n".join(f"  - {verdicts[p.point_id][1]}" for p in unsafe[:5])
        more = f"\n  ... and {len(unsafe) - 5} more" if len(unsafe) > 5 else ""
        raise UnsafePollInterval(
            f"{len(unsafe)} point(s) cannot be safely collected at a "
            f"{cfg.poll_interval_s}s poll interval",
            f"{detail}{more}\n\n"
            "This is refused rather than warned about because the failure is silent and "
            "permanent: the station overwrites records before we collect them, nothing "
            "logs an error, and it surfaces months later as holes in a chart.\n\n"
            "Fix by lowering POLL_INTERVAL_S, raising the history capacity on the station, "
            "or — only if you have decided the loss is acceptable — setting "
            "ENFORCE_ROLL_GUARD=0.",
        )

    if unknown:
        log.warning(
            "%d of %d points have unknown roll horizon (capacity/interval not filled in "
            "from Workbench). Collecting them anyway, but we cannot tell whether this "
            "schedule loses data. Unknown is not the same as safe.",
            len(unknown), len(points),
        )

    run_id = repo.start_run(
        station_id, None, until, cfg.collector_host, cfg.version
    )

    log.info("Syncing %d point(s) up to %s", len(points), until.isoformat(timespec="seconds"))

    outcomes: list[SyncOutcome] = []
    for i, point in enumerate(points, 1):
        outcome = sync_point(client, repo, cfg, point, until, from_scratch)
        outcomes.append(outcome)
        if outcome.records_written:
            log.info(
                "  [%d/%d] %s: +%d records", i, len(points), point.name, outcome.records_written
            )
        elif i % 25 == 0:
            log.info("  [%d/%d] ...", i, len(points))

    written = sum(o.records_written for o in outcomes)
    succeeded = sum(1 for o in outcomes if o.ok)
    errors = [{"point": o.point_name, "error": o.error} for o in outcomes if not o.ok]
    gaps = [o for o in outcomes if o.gap_recorded]

    status = "ok" if not errors else ("partial" if succeeded else "failed")
    repo.finish_run(run_id, status, len(points), succeeded, written, errors)

    return {
        "run_id": run_id,
        "points": len(points),
        "succeeded": succeeded,
        "failed": len(errors),
        "written": written,
        "requests": sum(o.requests_made for o in outcomes),
        "gaps": len(gaps),
        "unknown_horizon": len(unknown),
        "status": status,
    }
