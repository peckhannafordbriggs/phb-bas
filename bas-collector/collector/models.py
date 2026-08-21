"""
The domain contract.

Nothing outside obix.py may see oBIX XML, a Niagara ORD, or a $-escaped name.
Everything downstream — the database layer, the sync loop, the CLI — works only
with the shapes in this file.

That boundary is the whole reason a second source (a Supervisor, Niagara Data
Service, or a different BAS vendor entirely) is a new adapter module rather than
a rewrite. It costs almost nothing to maintain and it is the single decision most
likely to matter in a year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StationInfo:
    """What /obix/about reports. Obtainable with only a station login."""

    station_name: str | None = None
    product_name: str | None = None
    product_version: str | None = None
    vendor_name: str | None = None
    timezone: str | None = None
    server_time: str | None = None


@dataclass
class HistoryMeta:
    """
    One history as the station describes it.

    Note what is absent: capacity and full_policy. oBIX does not expose the
    Niagara history extension's configuration, only the history itself. Those
    two numbers determine the roll horizon, and they have to come from Workbench
    or BQL. The collector treats them as unknown until a human fills them in,
    and unknown is never treated as safe.
    """

    station: str
    name: str                       # verbatim, $-escaped — this goes in the URL
    display_name: str               # decoded, for humans only
    count: int | None = None
    start: datetime | None = None   # oldest record the station still holds
    end: datetime | None = None     # newest record
    timezone: str | None = None     # IANA, from the station
    query_href: str | None = None   # discovered, not assumed to be ~historyQuery

    # Filled in by a limit=1 probe during discovery, from the #RecordDef prototype.
    unit: str | None = None
    data_type: str = "unknown"

    def implied_interval_s(self) -> int | None:
        """
        Rough collection interval derived from count/start/end.

        A hint, not a measurement — it is only the true interval if collection
        has been continuous, and the span only equals the roll horizon if the
        history is actually full. Never use this in place of the real capacity
        and interval from Workbench.
        """
        if not (self.count and self.start and self.end) or self.count < 2:
            return None
        span = (self.end - self.start).total_seconds()
        return int(span / (self.count - 1)) if span > 0 else None


@dataclass(frozen=True)
class TrendRecord:
    """
    One trend record, normalized.

    ts is always timezone-aware. The collector converts to UTC before writing;
    the station's IANA zone is kept separately on the point so local-time
    analysis stays possible without storing local timestamps.
    """

    ts: datetime
    value_num: float | None = None
    value_bool: bool | None = None
    value_str: str | None = None
    status: str | None = None

    @property
    def is_null(self) -> bool:
        """
        True when the station returned a record with no value.

        This is NOT the same as the record being absent. A null record means the
        station was logging and had nothing valid to log — a sensor fault, or a
        genuine gap in the measurement. An absent record means we never
        collected it. Conflating the two produces confident wrong answers about
        whether equipment was running.
        """
        return self.value_num is None and self.value_bool is None and self.value_str is None


@dataclass
class RecordDef:
    """The #RecordDef prototype: units and datatype, declared once per response."""

    data_type: str = "unknown"
    unit: str | None = None
    timezone: str | None = None


@dataclass
class QueryResult:
    definition: RecordDef = field(default_factory=RecordDef)
    records: list[TrendRecord] = field(default_factory=list)
    reported_count: int | None = None


@dataclass
class SyncOutcome:
    """Per-point result of one sync pass, aggregated into the ingest_run audit row."""

    point_id: int
    point_name: str
    records_written: int = 0
    requests_made: int = 0
    new_checkpoint: datetime | None = None
    skipped: bool = False
    error: str | None = None
    gap_recorded: tuple[datetime, datetime] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CollectorError(Exception):
    """Base class. Carries a diagnosis, not just a message."""

    def __init__(self, message: str, diagnosis: str = "", cause: Any = None):
        super().__init__(message)
        self.diagnosis = diagnosis
        self.cause = cause


class StationError(CollectorError):
    """Talking to the Niagara station failed."""


class UnsafePollInterval(CollectorError):
    """
    The configured poll interval is too slow for a point's roll horizon.

    Raised deliberately rather than warned about, because the failure mode it
    prevents is silent and permanent: the station overwrites records before we
    collect them, no error appears anywhere, and the gap is discovered months
    later as an unexplained hole in a chart.
    """
