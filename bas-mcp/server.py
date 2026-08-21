#!/usr/bin/env python3
"""
BAS MCP server — lets Claude answer questions about building data.

The design principle, which everything here follows:

    Claude decides WHAT to compute. These tools compute it. Claude explains
    the result. Claude never does arithmetic on trend rows.

That is not a stylistic preference. An LLM handed 50,000 temperature readings
and asked for an average will produce a number that looks right and is wrong,
with no indication anything went astray. So the tools aggregate in SQL and
return summaries. `get_readings` will not hand back thousands of rows even if
asked — it buckets automatically and says so.

Everything is read-only, enforced three ways: a read-only Postgres role, a
read-only transaction per query, and a validator that rejects anything that is
not a SELECT. Building data is irreplaceable — the JACE overwrites its own
history within about two days — so there is no undo for a bad DELETE here.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# The SDK renamed FastMCP to MCPServer in mcp 2.0. Same decorator API, different
# import path — support both so this does not break on whichever version happens
# to be installed.
try:
    from mcp.server import MCPServer as _Server  # mcp >= 2.0
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x

# Deliberately not named `mcp` — that would shadow the package it came from.
app = _Server("bas")

DSN = os.environ.get("BAS_READONLY_URL") or os.environ.get("DATABASE_URL")
STATEMENT_TIMEOUT_MS = int(os.environ.get("BAS_STATEMENT_TIMEOUT_MS", "15000"))
MAX_ROWS = int(os.environ.get("BAS_MAX_ROWS", "500"))


# ---------------------------------------------------------------------------
# Connection handling — read-only by construction
# ---------------------------------------------------------------------------

def _connect() -> psycopg.Connection:
    if not DSN:
        raise RuntimeError(
            "No database URL. Set BAS_READONLY_URL (preferred) or DATABASE_URL "
            "in the MCP server's environment."
        )
    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
    # Belt and braces: even if the role has write permission, the session does not.
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def _fetch(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return conn.execute(sql, params).fetchall()


def _fmt(rows: list[dict], empty: str = "No results.") -> str:
    """Render rows as a compact text table. Small enough to read, wide enough to be useful."""
    if not rows:
        return empty
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(_cell(r[c])) for r in rows)) for c in cols}
    out = ["  ".join(c.ljust(widths[c]) for c in cols),
           "  ".join("-" * widths[c] for c in cols)]
    for r in rows:
        out.append("  ".join(_cell(r[c]).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@app.tool()
def list_points(site: str | None = None, role: str | None = None) -> str:
    """
    List the points available, with what each one measures and where it lives.

    Start here when you do not yet know what data exists. Returns point names,
    their semantic role, units, equipment, and building.

    Args:
        site: optional building name filter (partial match, case-insensitive)
        role: optional point_role filter, e.g. 'supply_air_temp' or 'zone_temp'
    """
    sql = """
        SELECT point_name, point_role, unit, data_type,
               equipment_name, site_name, niagara_station_name
        FROM bas.v_point
        WHERE is_active
    """
    params: list = []
    if site:
        sql += " AND site_name ILIKE %s"
        params.append(f"%{site}%")
    if role:
        sql += " AND point_role = %s"
        params.append(role)
    sql += " ORDER BY site_name, equipment_name NULLS LAST, point_name"

    rows = _fetch(sql, tuple(params))
    if not rows:
        return "No points match. Try list_points() with no filters to see everything."

    unclassified = sum(1 for r in rows if not r["point_role"])
    note = ""
    if unclassified:
        note = (
            f"\n\nNOTE: {unclassified} of {len(rows)} points have no point_role assigned. "
            "Those are invisible to any question phrased in terms of what a point measures "
            "(e.g. 'compare supply air temperature across units') — they can only be reached "
            "by their exact name."
        )
    return f"{len(rows)} active point(s):\n\n{_fmt(rows)}{note}"


@app.tool()
def list_roles() -> str:
    """
    List the point_role vocabulary — the controlled set of things a point can measure.

    Useful for translating a plain-English question into a filter. "Supply air
    temperature" maps to point_role='supply_air_temp', and that works across every
    building regardless of how the installer named the point.
    """
    rows = _fetch("""
        SELECT r.point_role, r.display_name, r.measurement, r.typical_unit,
               count(p.point_id) AS points_in_use
        FROM bas.point_role r
        LEFT JOIN bas.point p ON p.point_role = r.point_role AND p.is_active
        GROUP BY 1,2,3,4
        HAVING count(p.point_id) > 0
        ORDER BY 1
    """)
    if not rows:
        return (
            "No roles are in use yet — no point has been classified.\n\n"
            "The vocabulary exists (91 roles are defined) but nothing has been assigned to it. "
            "Until points are classified, questions must name points explicitly."
        )
    return f"Roles currently in use:\n\n{_fmt(rows)}"


@app.tool()
def describe_schema() -> str:
    """
    Return the annotated database schema — every table, view, column and its documented meaning.

    Read this before writing SQL with run_sql. The column comments explain things
    that are not obvious from names, including which relations to prefer and what
    a NULL actually signifies in each context.
    """
    rows = _fetch("""
        SELECT object_name, object_type, column_name, data_type, column_description
        FROM bas.v_data_dictionary
        ORDER BY object_type DESC, object_name, column_name
    """)
    out: list[str] = []
    current = None
    for r in rows:
        if r["object_name"] != current:
            current = r["object_name"]
            out.append(f"\n## bas.{current}  ({r['object_type']})")
        desc = f"  — {r['column_description']}" if r["column_description"] else ""
        out.append(f"  {r['column_name']} : {r['data_type']}{desc}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Trend data — aggregated, never dumped
# ---------------------------------------------------------------------------

@app.tool()
def get_readings(point_name: str, hours: int = 24, max_buckets: int = 100) -> str:
    """
    Get trend data for one point over a time window, automatically aggregated.

    This deliberately does NOT return raw rows for large windows. If the window
    contains more readings than max_buckets, it buckets them by time and returns
    min/avg/max per bucket, and tells you it did so. Reasoning over 5,000 raw
    numbers is exactly how a confident wrong answer gets produced.

    For an exact figure over a period, use summarize_point — it computes in SQL.

    Args:
        point_name: the point to fetch (partial match, case-insensitive)
        hours: how far back to look, default 24
        max_buckets: maximum rows to return, default 100
    """
    pt = _resolve_point(point_name)
    if isinstance(pt, str):
        return pt

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    total = _fetch(
        "SELECT count(*) AS n FROM bas.reading WHERE point_id=%s AND ts BETWEEN %s AND %s",
        (pt["point_id"], start, end),
    )[0]["n"]

    if total == 0:
        return (
            f"No readings for '{pt['point_name']}' in the last {hours}h.\n\n"
            "Either the point has not logged recently, or collection has not run over that "
            "window. Check collection_health()."
        )

    if total <= max_buckets:
        rows = _fetch("""
            SELECT ts_local, value_num, value_bool, value_str, status
            FROM bas.v_reading
            WHERE point_id=%s AND ts BETWEEN %s AND %s
            ORDER BY ts
        """, (pt["point_id"], start, end))
        header = f"{pt['point_name']} — {total} readings over {hours}h (all shown, local time)"
        return f"{header}\n\n{_fmt(rows)}"

    bucket_s = max(60, int((hours * 3600) / max_buckets))
    rows = _fetch("""
        SELECT date_bin(make_interval(secs => %s), r.ts, TIMESTAMPTZ '2000-01-01')
                 AT TIME ZONE s.timezone            AS bucket_local,
               round(avg(r.value_num)::numeric, 2)  AS avg,
               round(min(r.value_num)::numeric, 2)  AS min,
               round(max(r.value_num)::numeric, 2)  AS max,
               count(*)                             AS n
        FROM bas.reading r
        JOIN bas.point p   ON p.point_id = r.point_id
        JOIN bas.station st ON st.station_id = p.station_id
        JOIN bas.site s     ON s.site_id = st.site_id
        WHERE r.point_id = %s AND r.ts BETWEEN %s AND %s
        GROUP BY 1, s.timezone
        ORDER BY 1
    """, (bucket_s, pt["point_id"], start, end))

    unit = f" ({pt['unit']})" if pt["unit"] else ""
    return (
        f"{pt['point_name']}{unit} — {total} readings over {hours}h, "
        f"aggregated into {len(rows)} buckets of {bucket_s}s (local time).\n"
        f"Raw rows were not returned: reasoning over {total} individual numbers would be "
        f"unreliable. Use summarize_point for exact figures.\n\n{_fmt(rows)}"
    )


@app.tool()
def summarize_point(point_name: str, days: int = 7) -> str:
    """
    Statistical summary for one point, computed in the database.

    Use this for any question about averages, ranges, or how much data exists.
    The arithmetic happens in SQL, so the numbers are correct.

    Args:
        point_name: the point to summarise (partial match, case-insensitive)
        days: how far back, default 7
    """
    pt = _resolve_point(point_name)
    if isinstance(pt, str):
        return pt

    start = datetime.now(timezone.utc) - timedelta(days=days)
    r = _fetch("""
        SELECT count(*)                                        AS readings,
               count(*) FILTER (WHERE value_num IS NULL
                            AND value_bool IS NULL
                            AND value_str IS NULL)             AS null_records,
               round(avg(value_num)::numeric, 2)               AS avg,
               round(min(value_num)::numeric, 2)               AS min,
               round(max(value_num)::numeric, 2)               AS max,
               round(stddev_samp(value_num)::numeric, 3)       AS stddev,
               min(ts)                                         AS first_reading,
               max(ts)                                         AS last_reading
        FROM bas.reading WHERE point_id=%s AND ts >= %s
    """, (pt["point_id"], start))[0]

    if not r["readings"]:
        return f"No readings for '{pt['point_name']}' in the last {days} days."

    unit = pt["unit"] or "(no unit recorded)"
    lines = [
        f"{pt['point_name']} — last {days} days",
        f"  role         {pt['point_role'] or '(unclassified)'}",
        f"  unit         {unit}",
        f"  readings     {r['readings']:,}",
        f"  average      {r['avg']}",
        f"  range        {r['min']} to {r['max']}",
        f"  std dev      {r['stddev']}",
        f"  first        {r['first_reading']}",
        f"  last         {r['last_reading']}",
    ]
    if r["null_records"]:
        lines.append(
            f"  null records {r['null_records']}  <- station logged these with no value "
            f"(sensor fault or real gap). Not the same as missing rows."
        )
    if r["stddev"] is not None and float(r["stddev"] or 0) == 0:
        lines.append(
            "\n  WARNING: standard deviation is zero — this value never changed over the "
            "period. That is a classic stuck-sensor signature, not a stable building."
        )

    gaps = _fetch("""
        SELECT gap_start, gap_end, cause FROM bas.data_gap
        WHERE point_id=%s AND gap_end >= %s ORDER BY gap_start
    """, (pt["point_id"], start))
    if gaps:
        lines.append(f"\n  {len(gaps)} recorded data gap(s) in this window:")
        lines.append(_fmt(gaps))
        lines.append("  Do not read these as equipment being off — they are periods we did not collect.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fault detection — deterministic rules, not inference
# ---------------------------------------------------------------------------

@app.tool()
def find_faults(days: int = 7) -> str:
    """
    Run deterministic HVAC fault rules over recent data.

    These are rules, not machine learning, and that is deliberate — the classic
    expensive building faults are all deterministic and explainable. Each rule
    below is a real failure mode that is invisible on an alarm screen and obvious
    in trend data.

    Rules that need point_role assigned and equipment linked will silently find
    nothing if points are unclassified. list_points() shows what is classified.

    Args:
        days: how far back to look, default 7
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    findings: list[str] = []

    # 1. Never reaching setpoint
    rows = _fetch("""
        SELECT sp.equipment_name,
               sp.measured_point_name,
               round(avg(m.value_num - s.value_num)::numeric, 1) AS avg_deviation,
               count(*) FILTER (WHERE abs(m.value_num - s.value_num) > 2) AS intervals_off
        FROM bas.v_setpoint_pair sp
        JOIN bas.v_reading m ON m.point_id = sp.measured_point_id
        JOIN bas.v_reading s ON s.point_id = sp.setpoint_point_id AND s.ts = m.ts
        WHERE m.ts >= %s
        GROUP BY 1,2
        HAVING count(*) FILTER (WHERE abs(m.value_num - s.value_num) > 2) > 0
        ORDER BY 4 DESC
    """, (since,))
    if rows:
        findings.append(
            "NOT REACHING SETPOINT — the unit is calling for conditioning it cannot deliver:\n"
            + _fmt(rows))

    # 2. Commanded on, not running
    rows = _fetch("""
        SELECT cs.equipment_name, cs.command_point_name, count(*) AS intervals
        FROM bas.v_command_status_pair cs
        JOIN bas.v_reading c ON c.point_id = cs.command_point_id
        JOIN bas.v_reading s ON s.point_id = cs.status_point_id AND s.ts = c.ts
        WHERE c.value_bool IS TRUE AND s.value_bool IS FALSE AND c.ts >= %s
        GROUP BY 1,2 ORDER BY 3 DESC
    """, (since,))
    if rows:
        findings.append(
            "COMMANDED ON BUT NOT RUNNING — the control system believes this is running and "
            "it is not, so everything downstream is being controlled on a false premise:\n"
            + _fmt(rows))

    # 3. Simultaneous heating and cooling
    rows = _fetch("""
        SELECT clg.equipment_name, count(*) AS intervals,
               round(avg(clg.value_num)::numeric, 0) AS avg_cooling_pct,
               round(avg(htg.value_num)::numeric, 0) AS avg_heating_pct
        FROM bas.v_reading clg
        JOIN bas.v_reading htg
          ON htg.equipment_id = clg.equipment_id AND htg.ts = clg.ts
         AND htg.point_role IN ('heating_valve_cmd','reheat_valve_cmd')
        WHERE clg.point_role = 'cooling_valve_cmd'
          AND clg.value_num > 5 AND htg.value_num > 5 AND clg.ts >= %s
        GROUP BY 1 ORDER BY 2 DESC
    """, (since,))
    if rows:
        findings.append(
            "SIMULTANEOUS HEATING AND COOLING — you pay to heat the air and pay again to "
            "cool it back down:\n" + _fmt(rows))

    # 4. Stuck sensors.
    #
    # Deliberately excludes setpoints and commands. A setpoint holding steady at
    # 55 degF is a setpoint working correctly, and a heating valve sitting closed
    # through August is correct behaviour — neither is a fault. Flagging them is
    # how a fault system teaches people to ignore it.
    #
    # Only MEASUREMENTS are checked, because only measurements are sensing a
    # physical world that always has some noise in it. A perfectly flat
    # measurement is a failed sensor or a frozen point.
    #
    # Unclassified points are included, since a stuck one is worth knowing about
    # even if we cannot yet tell what it is — but they are reported separately so
    # the difference in confidence is visible.
    # Detecting "stuck" is harder than it looks. Requiring variance of exactly
    # zero misses the common case: a sensor that has failed but whose reading
    # still carries a little quantization noise, so it wobbles by 0.08 and never
    # actually moves. A real example on this data sat at 64.5 degF with a
    # standard deviation of 0.08 for a full day and slipped straight through a
    # zero-variance test.
    #
    # A threshold on standard deviation does not work either, because it is
    # unit-dependent: 0.5 is nothing for a pressure in pascals and enormous for
    # a valve position in percent, and the rule has to work on any point in any
    # building without per-point tuning.
    #
    # Distinct value count is unit-independent and works. A live sensor sampling
    # a physical quantity produces many distinct values; a dead one produces a
    # handful regardless of what it measures or what scale it is on.
    #
    # Both tests are reported with the evidence that fired, so the difference in
    # confidence stays visible rather than being flattened into one verdict.
    rows = _fetch("""
        SELECT p.display_name AS point_name,
               COALESCE(p.point_role, '(unclassified)') AS point_role,
               round(avg(r.value_num)::numeric, 2)      AS value,
               count(*)                                 AS readings,
               count(DISTINCT r.value_num)              AS distinct_values,
               round(coalesce(stddev_samp(r.value_num), 0)::numeric, 4) AS std_dev,
               CASE
                   WHEN coalesce(stddev_samp(r.value_num), 0) = 0
                       THEN 'frozen - value never changed at all'
                   ELSE 'barely moving - only a few distinct values'
               END AS evidence
        FROM bas.reading r
        JOIN bas.point p USING (point_id)
        LEFT JOIN bas.point_role pr ON pr.point_role = p.point_role
        WHERE r.ts >= %s
          AND r.value_num IS NOT NULL
          AND p.is_active
          AND (p.point_role IS NULL
               OR (NOT pr.is_setpoint AND NOT pr.is_command AND NOT pr.is_status))
        GROUP BY 1,2
        HAVING count(*) >= 20
           AND (coalesce(stddev_samp(r.value_num), 0) = 0
                OR (count(DISTINCT r.value_num) <= 3 AND count(*) >= 50))
        ORDER BY 7, 1
    """, (since,))
    if rows:
        findings.append(
            "STUCK SENSOR — this measurement is not tracking anything. A live sensor sampling "
            "the physical world produces many distinct values; a handful over hundreds of "
            "readings means it has stopped responding. Setpoints, commands and status points "
            "are excluded, since those are supposed to hold steady.\n"
            "Check distinct_values against readings — that ratio is the evidence, and it does "
            "not depend on units or scale:\n"
            + _fmt(rows))

    # 5. Running while unoccupied
    rows = _fetch("""
        SELECT equipment_name, point_name, count(*) AS intervals
        FROM bas.v_reading
        WHERE point_role IN ('supply_fan_status','return_fan_status','exhaust_fan_status',
                             'pump_status','chiller_status','boiler_status')
          AND value_bool IS TRUE
          AND (local_hour < 6 OR local_hour >= 19)
          AND ts >= %s
        GROUP BY 1,2 HAVING count(*) > 5 ORDER BY 3 DESC
    """, (since,))
    if rows:
        findings.append(
            "RUNNING OUTSIDE OCCUPIED HOURS (before 06:00 or after 19:00 local) — may be "
            "intentional, but is worth confirming against the schedule:\n" + _fmt(rows))

    if not findings:
        classified = _fetch(
            "SELECT count(*) AS n FROM bas.point WHERE is_active AND point_role IS NOT NULL"
        )[0]["n"]
        active = _fetch("SELECT count(*) AS n FROM bas.point WHERE is_active")[0]["n"]
        msg = f"No faults detected in the last {days} days."
        if classified < active:
            msg += (
                f"\n\nCaveat worth stating: only {classified} of {active} active points have a "
                "point_role assigned, and only classified points linked to equipment can be "
                "checked by most of these rules. A clean result here is weaker evidence than "
                "it looks."
            )
        return msg

    return f"Findings over the last {days} days:\n\n" + "\n\n".join(findings)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@app.tool()
def collection_health() -> str:
    """
    Is data actually arriving, and are we at risk of losing any?

    Check this before drawing conclusions from an apparent absence of data — a
    quiet point may mean the collector stopped, not that the equipment did.
    """
    counts = _fetch("""
        SELECT (SELECT count(*) FROM bas.point WHERE is_active)  AS active_points,
               (SELECT count(*) FROM bas.reading)                AS readings,
               (SELECT count(*) FROM bas.point
                 WHERE is_active AND point_role IS NULL)         AS unclassified,
               (SELECT count(*) FROM bas.data_gap)               AS gaps
    """)[0]

    risk = _fetch("""
        SELECT roll_risk, count(*) AS points FROM bas.v_collection_health
        WHERE is_active GROUP BY 1 ORDER BY 2 DESC
    """)
    stale = _fetch("""
        SELECT point_name, last_record_ts, roll_risk,
               round(seconds_since_last_record / 3600.0, 1) AS hours_since
        FROM bas.v_collection_health
        WHERE is_active AND roll_risk IN ('data_lost','at_risk','never_collected')
        ORDER BY seconds_since_last_record DESC NULLS FIRST LIMIT 10
    """)
    runs = _fetch("""
        SELECT started_at, status, points_succeeded, points_attempted, records_written
        FROM bas.ingest_run ORDER BY started_at DESC LIMIT 5
    """)

    out = [
        f"active points   {counts['active_points']}",
        f"readings        {counts['readings']:,}",
        f"unclassified    {counts['unclassified']}",
        f"recorded gaps   {counts['gaps']}",
        "",
        "collection risk:",
        _fmt(risk, "  (no points)"),
    ]
    if stale:
        out += ["", "points needing attention:", _fmt(stale)]
    if runs:
        out += ["", "recent collector runs:", _fmt(runs)]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# SQL escape hatch — guarded
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|reindex|cluster|refresh|call|do|set|reset|lock|listen|notify)\b",
    re.IGNORECASE,
)


@app.tool()
def run_sql(query: str) -> str:
    """
    Run a read-only SQL query for anything the other tools do not cover.

    Call describe_schema() first so you are querying documented columns rather
    than guessing from names.

    Prefer the views over the base tables: bas.v_reading has equipment, site,
    units and building-local time already joined on every row, which avoids the
    multi-table joins that are easy to get subtly wrong.

    Only SELECT and WITH are permitted. Writes are rejected here, and the
    database connection is read-only regardless — building history cannot be
    recovered once destroyed, since the controller overwrites its own copy
    within about two days.

    Args:
        query: a single SELECT or WITH statement
    """
    sql = query.strip().rstrip(";").strip()

    if not sql:
        return "Empty query."
    if ";" in sql:
        return (
            "Rejected: multiple statements. Send one SELECT at a time — stacked statements "
            "are how a read-only guard gets bypassed."
        )
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return "Rejected: only SELECT and WITH queries are permitted."
    if _FORBIDDEN.search(sql):
        return (
            "Rejected: the query contains a keyword that could modify data or session state. "
            "This tool is strictly read-only."
        )

    limited = sql if re.search(r"\blimit\s+\d+\s*$", sql, re.IGNORECASE) else f"{sql} LIMIT {MAX_ROWS}"

    try:
        rows = _fetch(limited)
    except psycopg.errors.QueryCanceled:
        return (
            f"Query exceeded the {STATEMENT_TIMEOUT_MS}ms timeout. Narrow the time range, or "
            "aggregate in SQL rather than returning rows."
        )
    except psycopg.Error as exc:
        return f"SQL error: {exc}"

    if len(rows) >= MAX_ROWS:
        return (
            f"{_fmt(rows)}\n\n"
            f"TRUNCATED at {MAX_ROWS} rows. There is more data than this. Aggregate in SQL "
            f"(GROUP BY, avg, count) rather than paging — conclusions drawn from a truncated "
            f"result set are wrong in a way that is invisible."
        )
    return _fmt(rows)


# ---------------------------------------------------------------------------

def _resolve_point(name: str) -> dict | str:
    """Find one point by fuzzy name, or explain what went wrong."""
    rows = _fetch("""
        SELECT point_id, point_name, point_role, unit, site_name
        FROM bas.v_point
        WHERE is_active AND (point_name ILIKE %s OR niagara_history_name ILIKE %s)
        ORDER BY length(point_name) LIMIT 10
    """, (f"%{name}%", f"%{name}%"))

    if not rows:
        return f"No active point matches '{name}'. Use list_points() to see what exists."
    if len(rows) > 1 and rows[0]["point_name"].lower() != name.lower():
        names = ", ".join(r["point_name"] for r in rows)
        return f"'{name}' matches several points: {names}. Be more specific."
    return rows[0]


if __name__ == "__main__":
    app.run()
