"""Configuration, all from the environment. Nothing secret is ever hardcoded."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional
    pass

VERSION = "0.1.0"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # -- Niagara --
    base_url: str
    username: str | None
    password: str | None
    cookie: str | None
    verify_tls: bool
    timeout_s: float

    # -- Database --
    database_url: str

    # -- Where this station sits in the hierarchy --
    org_name: str
    site_name: str
    site_timezone: str

    # -- Collection behaviour --
    poll_interval_s: int
    max_window_hours: int
    max_records_per_request: int
    initial_backfill_days: int
    enforce_roll_guard: bool
    roll_safety_factor: int

    collector_host: str = socket.gethostname()
    version: str = VERSION

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ.get("NIAGARA_BASE_URL", "").strip()
        database_url = os.environ.get("DATABASE_URL", "").strip()

        missing = [
            n
            for n, v in (("NIAGARA_BASE_URL", base_url), ("DATABASE_URL", database_url))
            if not v
        ]
        if missing:
            raise SystemExit(
                f"Missing required setting(s): {', '.join(missing)}.\n"
                "Copy .env.example to .env and fill it in."
            )

        return cls(
            base_url=base_url,
            username=os.environ.get("NIAGARA_USER") or None,
            password=os.environ.get("NIAGARA_PASS") or None,
            cookie=os.environ.get("NIAGARA_COOKIE") or None,
            verify_tls=_bool("NIAGARA_VERIFY_TLS", True),
            timeout_s=float(_int("NIAGARA_TIMEOUT_S", 30)),
            database_url=database_url,
            org_name=os.environ.get("BAS_ORG_NAME", "PHB"),
            site_name=os.environ.get("BAS_SITE_NAME", "Unnamed Building"),
            site_timezone=os.environ.get("BAS_SITE_TIMEZONE", "America/New_York"),
            poll_interval_s=_int("POLL_INTERVAL_S", 900),
            max_window_hours=_int("MAX_WINDOW_HOURS", 24),
            max_records_per_request=_int("MAX_RECORDS_PER_REQUEST", 1000),
            initial_backfill_days=_int("INITIAL_BACKFILL_DAYS", 30),
            # A point whose roll horizon is unknown, or shorter than
            # poll_interval * safety_factor, is unsafe to collect on this
            # schedule. Default 4 means we want at least four polls' worth of
            # margin before the station starts overwriting.
            enforce_roll_guard=_bool("ENFORCE_ROLL_GUARD", True),
            roll_safety_factor=_int("ROLL_SAFETY_FACTOR", 4),
        )

    @property
    def auth_kind(self) -> str:
        if self.cookie:
            return "cookie"
        if self.username:
            return "basic"
        return "none"
