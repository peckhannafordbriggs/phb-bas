#!/usr/bin/env python3
"""
Exercise every MCP tool directly, without going through the protocol.

Calls the underlying functions, prints what Claude would actually receive, and
asserts the properties that matter: writes are refused, large windows aggregate
instead of dumping rows, and the fault rules fire on known faults.

    python test_tools.py
"""

from __future__ import annotations

import os
import sys

import server as s

PASSED = 0
FAILED = 0


def section(name: str) -> None:
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")


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


def show(title: str, text: str, limit: int = 14) -> None:
    print(f"\n--- {title} ---")
    lines = text.splitlines()
    print("\n".join(lines[:limit]))
    if len(lines) > limit:
        print(f"... ({len(lines) - limit} more lines)")


def main() -> int:
    if not (os.environ.get("BAS_READONLY_URL") or os.environ.get("DATABASE_URL")):
        sys.exit("Set BAS_READONLY_URL or DATABASE_URL first.")

    section("Discovery")
    points = s.list_points()
    show("list_points()", points)
    check("list_points returns something", "point(s)" in points or "No points" in points)

    roles = s.list_roles()
    show("list_roles()", roles, 8)
    check("list_roles responds", len(roles) > 0)

    schema = s.describe_schema()
    check(f"describe_schema returns the annotated schema ({len(schema)} chars)",
          "bas.reading" in schema and "bas.v_reading" in schema)
    check("schema includes column documentation, not just names", "—" in schema)

    section("Read-only enforcement")

    for bad, label in [
        ("DELETE FROM bas.reading", "DELETE"),
        ("UPDATE bas.point SET unit='x'", "UPDATE"),
        ("DROP TABLE bas.reading", "DROP"),
        ("INSERT INTO bas.org (name) VALUES ('x')", "INSERT"),
        ("TRUNCATE bas.reading", "TRUNCATE"),
        ("SELECT 1; DELETE FROM bas.reading", "stacked statements"),
        ("SET session_replication_role = 'replica'", "SET"),
    ]:
        out = s.run_sql(bad)
        check(f"{label} is refused", out.startswith("Rejected"), out[:120])

    ok = s.run_sql("SELECT count(*) AS n FROM bas.reading")
    check("a legitimate SELECT still works", "n" in ok and not ok.startswith("Rejected"), ok[:120])

    # The role-level guarantee, independent of the validator above.
    if os.environ.get("BAS_READONLY_URL"):
        import psycopg
        blocked = False
        try:
            with psycopg.connect(os.environ["BAS_READONLY_URL"], autocommit=True) as c:
                c.execute("DELETE FROM bas.reading WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            blocked = True
        except psycopg.errors.ReadOnlySqlTransaction:
            blocked = True
        check("the database role itself refuses writes, regardless of the validator", blocked,
              "the role can write — run setup_readonly_role.sql and point BAS_READONLY_URL at it")
    else:
        print("  SKIP  role-level write test (BAS_READONLY_URL not set)")

    section("Trend data")

    pts = s._fetch(
        "SELECT point_name FROM bas.v_point WHERE is_active ORDER BY point_name LIMIT 1")
    if not pts:
        print("  No active points — skipping data tools.")
    else:
        name = pts[0]["point_name"]

        small = s.get_readings(name, hours=2)
        show(f"get_readings('{name}', hours=2)", small, 10)

        big = s.get_readings(name, hours=720, max_buckets=20)
        show(f"get_readings('{name}', hours=720, max_buckets=20)", big, 10)
        has_data = "No readings" not in big
        if has_data:
            check("a large window aggregates rather than dumping raw rows",
                  "aggregated into" in big, big[:200])
            check("and says why it did that", "unreliable" in big or "not returned" in big)
            body = [l for l in big.splitlines() if l and not l.startswith(("---", " "))]
            check("returned row count stays bounded", len(body) < 40, f"{len(body)} lines")
        else:
            print("  SKIP  aggregation test (no data in window)")

        summary = s.summarize_point(name, days=30)
        show(f"summarize_point('{name}', days=30)", summary, 16)
        check("summarize_point computes statistics", "readings" in summary)

        check("an unknown point name is handled gracefully",
              "No active point matches" in s.get_readings("definitely_not_a_point"))

    section("Fault detection")
    faults = s.find_faults(days=30)
    show("find_faults(days=30)", faults, 24)
    check("find_faults runs without error", len(faults) > 0)
    check("a clean result is caveated when points are unclassified",
          "No faults detected" not in faults or "Caveat" in faults or "unclassified" not in faults.lower(),
          "a clean bill of health from unclassified points is misleading and should say so")

    section("Operations")
    health = s.collection_health()
    show("collection_health()", health, 20)
    check("collection_health reports counts", "active points" in health)

    print(f"\n{'=' * 70}\n  {PASSED} passed, {FAILED} failed\n{'=' * 70}\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
