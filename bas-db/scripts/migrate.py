#!/usr/bin/env python3
"""
Migration runner.

Applies numbered .sql files from migrations/ in order, once each, recording what
it applied and a checksum of the file contents.

Why a runner at all, rather than just piping SQL into psql: because "what is the
schema right now" needs to be answerable a year from now without archaeology.
Ad-hoc ALTER statements typed into a terminal are how a database becomes
something nobody can reproduce.

The checksum matters more than it looks. If someone edits a migration that has
already run, this refuses to continue rather than silently leaving the database
in a state that does not match the file. Fix forward with a new migration; never
edit an applied one.

Usage:
    python scripts/migrate.py            apply pending migrations
    python scripts/migrate.py --status   show what is applied, change nothing
    python scripts/migrate.py --reset    DROP the bas schema and reapply from scratch
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:
    sys.exit("psycopg is not installed. Run:  pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # .env support is optional; environment variables still work.


ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit(
            "DATABASE_URL is not set.\n"
            "Copy .env.example to .env, or set the variable in your shell.\n"
            "Default for the bundled docker-compose:\n"
            "  postgresql://bas:bas_local_dev_only@localhost:5432/bas"
        )
    return url


def checksum(text: str) -> str:
    # Normalise line endings so a file edited on Windows and one edited on Linux
    # produce the same checksum. Without this, cloning the repo on another
    # machine looks like tampering.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


def discover() -> list[tuple[str, Path, str]]:
    if not MIGRATIONS_DIR.is_dir():
        sys.exit(f"No migrations directory at {MIGRATIONS_DIR}")
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        out.append((path.stem, path, checksum(text)))
    if not out:
        sys.exit(f"No .sql files found in {MIGRATIONS_DIR}")
    return out


def bootstrap(conn: psycopg.Connection) -> None:
    """Create the bookkeeping table. Must exist before any migration runs."""
    with conn.transaction():
        conn.execute("CREATE SCHEMA IF NOT EXISTS bas")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bas.schema_migration (
                version     text PRIMARY KEY,
                checksum    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def applied(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT version, checksum FROM bas.schema_migration ORDER BY version"
    ).fetchall()
    return {v: c for v, c in rows}


def cmd_status(conn: psycopg.Connection) -> int:
    done = applied(conn)
    print(f"\n{'migration':<28} {'status':<12} checksum")
    print("-" * 60)
    drift = 0
    for version, _path, current in discover():
        if version not in done:
            print(f"{version:<28} {'PENDING':<12} {current}")
        elif done[version] != current:
            print(f"{version:<28} {'CHANGED!':<12} {done[version]} -> {current}")
            drift += 1
        else:
            print(f"{version:<28} {'applied':<12} {current}")
    print()
    if drift:
        print(f"{drift} applied migration(s) have been edited since they ran.")
        print("Do not edit applied migrations. Write a new one that fixes forward.\n")
    return 1 if drift else 0


def cmd_reset(conn: psycopg.Connection) -> None:
    print("\nThis DROPs the entire bas schema and everything in it.")
    if input("Type 'reset' to confirm: ").strip() != "reset":
        sys.exit("Cancelled.")
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS bas CASCADE")
    print("Dropped.\n")


def cmd_migrate(conn: psycopg.Connection) -> int:
    done = applied(conn)
    pending = []

    for version, path, current in discover():
        if version in done:
            if done[version] != current:
                print(f"\nERROR: {version} has been edited since it was applied.")
                print(f"  applied: {done[version]}")
                print(f"  on disk: {current}")
                print("\nRefusing to continue. The database no longer matches the file,")
                print("and re-running would either fail or silently diverge.")
                print("Write a new migration that fixes forward instead.\n")
                return 1
            continue
        pending.append((version, path, current))

    if not pending:
        print("\nNothing to do — schema is up to date.\n")
        return 0

    print()
    for version, path, current in pending:
        sql = path.read_text(encoding="utf-8")
        print(f"  applying {version} ... ", end="", flush=True)
        try:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO bas.schema_migration (version, checksum) VALUES (%s, %s)",
                    (version, current),
                )
        except psycopg.Error as exc:
            print("FAILED")
            print(f"\n{exc}\n")
            print("The migration was rolled back; the database is unchanged.")
            print("Fix the SQL and run again.\n")
            return 1
        print("ok")

    print(f"\nApplied {len(pending)} migration(s).\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply BAS database migrations.")
    ap.add_argument("--status", action="store_true", help="show state, change nothing")
    ap.add_argument("--reset", action="store_true", help="drop the bas schema and reapply")
    args = ap.parse_args()

    try:
        conn = psycopg.connect(database_url(), autocommit=True)
    except psycopg.OperationalError as exc:
        print(f"\nCould not connect to the database.\n\n{exc}")
        print("Is Postgres running?  docker compose up -d\n")
        return 1

    with conn:
        if args.reset:
            cmd_reset(conn)
        bootstrap(conn)
        if args.status:
            return cmd_status(conn)
        return cmd_migrate(conn)


if __name__ == "__main__":
    sys.exit(main())
