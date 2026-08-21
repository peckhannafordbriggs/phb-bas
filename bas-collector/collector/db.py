"""
Database layer.

One rule shapes this whole module: **a checkpoint may never be ahead of
committed data.** Readings and the checkpoint that describes them are written in
the same transaction, so there is no window in which the collector believes it
has collected something it has not. Everything else about crash recovery follows
from that: kill the process at any moment and the next run resumes correctly,
because the checkpoint can only ever be behind, never ahead.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from .models import HistoryMeta, TrendRecord

BATCH = 1000


@dataclass
class PointRow:
    """A registered point, joined with its checkpoint."""

    point_id: int
    niagara_history_name: str
    display_name: str | None
    station_id: int
    niagara_station_name: str
    point_role: str | None
    unit: str | None
    data_type: str
    collection_interval_s: int | None
    capacity: int | None
    roll_horizon_s: int | None
    last_record_ts: datetime | None
    consecutive_failures: int

    @property
    def name(self) -> str:
        return self.display_name or self.niagara_history_name


def _chunks(seq: Sequence, size: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Repository:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        with self.conn.transaction():
            yield self.conn.cursor()

    # -- registration ------------------------------------------------------

    def ensure_site(self, org_name: str, site_name: str, site_timezone: str) -> int:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO bas.org (name) VALUES (%s) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING org_id",
                (org_name,),
            )
            org_id = cur.fetchone()["org_id"]

            cur.execute(
                "INSERT INTO bas.site (org_id, name, timezone) VALUES (%s,%s,%s) "
                "ON CONFLICT (org_id, name) DO UPDATE SET timezone = EXCLUDED.timezone "
                "RETURNING site_id",
                (org_id, site_name, site_timezone),
            )
            return cur.fetchone()["site_id"]

    def ensure_station(
        self,
        site_id: int,
        niagara_station_name: str,
        base_url: str | None = None,
        version: str | None = None,
    ) -> int:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO bas.station (site_id, niagara_station_name, base_url, niagara_version)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (site_id, niagara_station_name) DO UPDATE
                  SET base_url        = COALESCE(EXCLUDED.base_url, bas.station.base_url),
                      niagara_version = COALESCE(EXCLUDED.niagara_version, bas.station.niagara_version),
                      last_seen_at    = now()
                RETURNING station_id
                """,
                (site_id, niagara_station_name, base_url, version),
            )
            return cur.fetchone()["station_id"]

    def upsert_point(self, station_id: int, meta: HistoryMeta) -> tuple[int, bool]:
        """
        Register or refresh a point. Returns (point_id, was_created).

        Deliberately does NOT overwrite point_role, equipment_id, capacity, or
        collection_interval_s. Those are human judgement and Workbench facts —
        a rediscovery must never silently undo someone's classification work.
        """
        with self.transaction() as cur:
            cur.execute(
                "SELECT point_id FROM bas.point "
                "WHERE station_id = %s AND niagara_history_name = %s",
                (station_id, meta.name),
            )
            existing = cur.fetchone()

            cur.execute(
                """
                INSERT INTO bas.point
                  (station_id, niagara_history_name, display_name, unit, data_type,
                   source_timezone, last_seen_at)
                VALUES (%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (station_id, niagara_history_name) DO UPDATE
                  SET display_name    = COALESCE(EXCLUDED.display_name, bas.point.display_name),
                      unit            = COALESCE(EXCLUDED.unit, bas.point.unit),
                      data_type       = CASE WHEN EXCLUDED.data_type = 'unknown'
                                             THEN bas.point.data_type ELSE EXCLUDED.data_type END,
                      source_timezone = COALESCE(EXCLUDED.source_timezone, bas.point.source_timezone),
                      last_seen_at    = now(),
                      is_active       = true
                RETURNING point_id
                """,
                (
                    station_id,
                    meta.name,
                    meta.display_name,
                    meta.unit,
                    meta.data_type,
                    meta.timezone,
                ),
            )
            point_id = cur.fetchone()["point_id"]

            cur.execute(
                "INSERT INTO bas.sync_checkpoint (point_id) VALUES (%s) "
                "ON CONFLICT (point_id) DO NOTHING",
                (point_id,),
            )
            return point_id, existing is None

    def mark_missing_points_inactive(self, station_id: int, seen_names: set[str]) -> list[str]:
        """
        Flag points the station no longer reports.

        Never deletes. A history that disappears is usually a rename or a
        configuration change, and the historical data it produced is still valid
        and still ours. Deleting it would destroy data the station itself can no
        longer supply.
        """
        with self.transaction() as cur:
            cur.execute(
                "SELECT point_id, niagara_history_name FROM bas.point "
                "WHERE station_id = %s AND is_active",
                (station_id,),
            )
            gone = [
                r["niagara_history_name"]
                for r in cur.fetchall()
                if r["niagara_history_name"] not in seen_names
            ]
            if gone:
                cur.execute(
                    "UPDATE bas.point SET is_active = false "
                    "WHERE station_id = %s AND niagara_history_name = ANY(%s)",
                    (station_id, gone),
                )
            return gone

    # -- reading points ----------------------------------------------------

    def active_points(self, station_id: int | None = None) -> list[PointRow]:
        sql = """
            SELECT p.point_id, p.niagara_history_name, p.display_name, p.station_id,
                   st.niagara_station_name, p.point_role, p.unit, p.data_type,
                   p.collection_interval_s, p.capacity, p.roll_horizon_s,
                   c.last_record_ts, COALESCE(c.consecutive_failures, 0) AS consecutive_failures
            FROM bas.point p
            JOIN bas.station st ON st.station_id = p.station_id
            LEFT JOIN bas.sync_checkpoint c ON c.point_id = p.point_id
            WHERE p.is_active
        """
        params: tuple = ()
        if station_id is not None:
            sql += " AND p.station_id = %s"
            params = (station_id,)
        sql += " ORDER BY p.niagara_history_name"

        return [PointRow(**row) for row in self.conn.execute(sql, params).fetchall()]

    # -- the write path ----------------------------------------------------

    def write_batch(
        self,
        point_id: int,
        records: Sequence[TrendRecord],
        checkpoint_ts: datetime | None,
    ) -> int:
        """
        Write readings and advance the checkpoint IN ONE TRANSACTION.

        This is the crash-safety guarantee. If the process dies between the two,
        both roll back — the checkpoint cannot end up describing data that was
        never committed.
        """
        if not records and checkpoint_ts is None:
            return 0

        # Dedupe within the batch. ON CONFLICT handles it, but a station
        # returning the same timestamp twice is worth not relying on.
        seen: dict[datetime, TrendRecord] = {}
        for r in records:
            seen[r.ts.astimezone(timezone.utc)] = r
        unique = list(seen.values())

        written = 0
        with self.transaction() as cur:
            for chunk in _chunks(unique, BATCH):
                placeholders = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(chunk))
                args: list = []
                for r in chunk:
                    args += [
                        point_id,
                        r.ts.astimezone(timezone.utc),
                        r.value_num,
                        r.value_bool,
                        r.value_str,
                        r.status,
                    ]
                cur.execute(
                    "INSERT INTO bas.reading "
                    "(point_id, ts, value_num, value_bool, value_str, status) "
                    f"VALUES {placeholders} "
                    "ON CONFLICT (point_id, ts) DO NOTHING RETURNING 1",
                    args,
                )
                written += len(cur.fetchall())

            if checkpoint_ts is not None:
                cur.execute(
                    """
                    INSERT INTO bas.sync_checkpoint
                      (point_id, last_record_ts, last_run_at, last_status, consecutive_failures)
                    VALUES (%s,%s, now(), 'ok', 0)
                    ON CONFLICT (point_id) DO UPDATE
                      SET last_record_ts = GREATEST(
                              bas.sync_checkpoint.last_record_ts, EXCLUDED.last_record_ts),
                          last_run_at          = now(),
                          last_status          = 'ok',
                          consecutive_failures = 0,
                          last_error           = NULL
                    """,
                    (point_id, checkpoint_ts),
                )
        return written

    def mark_success(self, point_id: int) -> None:
        """
        Record a successful pass that produced no new records.

        Necessary because "nothing to collect" is a normal, healthy outcome — an
        idle point, or one already up to date. Without this, a point that fails
        once and then recovers keeps reporting last_status='error' forever,
        because write_batch only touches the checkpoint when there is something
        to write. That turns into permanent false alarms in v_collection_health
        and trains everyone to ignore it.

        Deliberately does NOT touch last_record_ts. Only committed data may move
        the high-water mark.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO bas.sync_checkpoint
                  (point_id, last_run_at, last_status, consecutive_failures)
                VALUES (%s, now(), 'ok', 0)
                ON CONFLICT (point_id) DO UPDATE
                  SET last_run_at          = now(),
                      last_status          = 'ok',
                      consecutive_failures = 0,
                      last_error           = NULL
                """,
                (point_id,),
            )

    def record_failure(self, point_id: int, error: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO bas.sync_checkpoint
                  (point_id, last_run_at, last_status, consecutive_failures, last_error)
                VALUES (%s, now(), 'error', 1, %s)
                ON CONFLICT (point_id) DO UPDATE
                  SET last_run_at          = now(),
                      last_status          = 'error',
                      consecutive_failures = bas.sync_checkpoint.consecutive_failures + 1,
                      last_error           = EXCLUDED.last_error
                """,
                (point_id, error[:2000]),
            )

    def record_gap(
        self, point_id: int, start: datetime, end: datetime, cause: str, notes: str | None = None
    ) -> None:
        """
        Record a period we know we are missing.

        Explicitly recording gaps is what lets analysis distinguish "the
        equipment was off" from "we were not looking". Without it, an AI asked
        why a unit stopped will confidently describe a shutdown that never
        happened.
        """
        if end <= start:
            return
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO bas.data_gap (point_id, gap_start, gap_end, cause, notes) "
                "VALUES (%s,%s,%s,%s,%s)",
                (point_id, start, end, cause, notes),
            )

    # -- audit -------------------------------------------------------------

    def start_run(
        self, station_id: int | None, window_start: datetime | None, window_end: datetime | None,
        collector_host: str, version: str,
    ) -> int:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO bas.ingest_run "
                "(station_id, window_start, window_end, collector_host, collector_version) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING run_id",
                (station_id, window_start, window_end, collector_host, version),
            )
            return cur.fetchone()["run_id"]

    def finish_run(
        self, run_id: int, status: str, attempted: int, succeeded: int,
        written: int, errors: list[dict],
    ) -> None:
        import json

        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE bas.ingest_run
                   SET finished_at = now(), status = %s, points_attempted = %s,
                       points_succeeded = %s, records_written = %s, errors = %s
                 WHERE run_id = %s
                """,
                (status, attempted, succeeded, written, json.dumps(errors), run_id),
            )

    # -- introspection -----------------------------------------------------

    def schema_present(self) -> bool:
        row = self.conn.execute(
            "SELECT to_regclass('bas.reading') IS NOT NULL AS present"
        ).fetchone()
        return bool(row and row["present"])

    def counts(self) -> dict:
        return self.conn.execute(
            """
            SELECT (SELECT count(*) FROM bas.point WHERE is_active) AS active_points,
                   (SELECT count(*) FROM bas.reading)               AS readings,
                   (SELECT count(*) FROM bas.point
                     WHERE point_role IS NULL AND is_active)        AS unclassified,
                   (SELECT count(*) FROM bas.data_gap)              AS gaps
            """
        ).fetchone()
