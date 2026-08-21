-- =============================================================================
-- 003_views.sql
--
-- The query surface.
--
-- These exist because of a specific, measurable problem: an LLM asked to write
-- SQL against a normalized schema gets the joins wrong. Not occasionally —
-- routinely, and in ways that produce plausible wrong numbers rather than
-- errors. A six-table join through station and org to get from a reading to a
-- building name is exactly the shape that goes wrong.
--
-- So the normalized tables stay normalized (they are correct, and they are what
-- the collector writes to), and these views give the analysis layer something
-- that reads like one flat table per question.
--
-- The last two views are the payoff from the point_role vocabulary: they turn
-- "did it reach setpoint" and "commanded on but not running" into things you can
-- select from, across every building, with no per-point configuration.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- v_point — every point with its full context
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_point AS
SELECT
    p.point_id,
    COALESCE(p.display_name, p.niagara_history_name) AS point_name,
    p.point_role,
    pr.display_name        AS point_role_name,
    pr.description         AS point_role_description,
    pr.measurement,
    pr.is_setpoint,
    pr.is_command,
    pr.is_status,
    pr.setpoint_for,
    pr.status_of,
    p.unit,
    p.data_type,

    e.equipment_id,
    e.name                 AS equipment_name,
    e.equip_type,
    et.display_name        AS equipment_type_name,
    parent.name            AS parent_equipment_name,

    s.site_id,
    s.name                 AS site_name,
    s.timezone             AS site_timezone,
    o.org_id,
    o.name                 AS org_name,

    st.station_id,
    st.niagara_station_name,
    p.niagara_history_name,

    p.collection_interval_s,
    p.capacity,
    p.full_policy,
    p.roll_horizon_s,
    p.is_active,
    p.first_seen_at,
    p.last_seen_at
FROM bas.point p
JOIN      bas.station        st     ON st.station_id     = p.station_id
JOIN      bas.site           s      ON s.site_id         = st.site_id
JOIN      bas.org            o      ON o.org_id          = s.org_id
LEFT JOIN bas.equipment      e      ON e.equipment_id    = p.equipment_id
LEFT JOIN bas.equipment      parent ON parent.equipment_id = e.parent_equipment_id
LEFT JOIN bas.equipment_type et     ON et.equip_type     = e.equip_type
LEFT JOIN bas.point_role     pr     ON pr.point_role     = p.point_role;

COMMENT ON VIEW bas.v_point IS
  'Every point with its equipment, building, station, and semantic role flattened into one '
  'row. Start here when answering "what points exist" or "which points measure X".';


-- -----------------------------------------------------------------------------
-- v_reading — the main analytical surface
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_reading AS
SELECT
    r.ts,
    (r.ts AT TIME ZONE s.timezone)                        AS ts_local,
    EXTRACT(HOUR FROM (r.ts AT TIME ZONE s.timezone))::int AS local_hour,
    EXTRACT(DOW  FROM (r.ts AT TIME ZONE s.timezone))::int AS local_dow,

    r.value_num,
    r.value_bool,
    r.value_str,
    r.status,

    p.point_id,
    COALESCE(p.display_name, p.niagara_history_name) AS point_name,
    p.point_role,
    p.unit,
    p.data_type,

    e.equipment_id,
    e.name    AS equipment_name,
    e.equip_type,

    s.site_id,
    s.name    AS site_name,
    s.timezone AS site_timezone,
    o.name    AS org_name
FROM bas.reading r
JOIN      bas.point     p  ON p.point_id     = r.point_id
JOIN      bas.station   st ON st.station_id  = p.station_id
JOIN      bas.site      s  ON s.site_id      = st.site_id
JOIN      bas.org       o  ON o.org_id       = s.org_id
LEFT JOIN bas.equipment e  ON e.equipment_id = p.equipment_id;

COMMENT ON VIEW bas.v_reading IS
'Trend data with full context on every row. This is the primary relation to query for
analysis. Always filter on ts (or point_id) — the underlying table is large and the planner
needs a bound.

ts is UTC. ts_local is the same instant in the building''s own timezone, which is the only
frame in which "overnight", "business hours", or "last Tuesday" mean anything. local_hour
and local_dow (0=Sunday) are precomputed for occupancy-shaped questions.

A row with all three value columns NULL is a record the station returned as null — a sensor
fault or a real gap. That is NOT the same as no row at all, which means we never collected
it. Check bas.data_gap before concluding equipment was off.';

COMMENT ON COLUMN bas.v_reading.ts_local IS
  'The reading instant expressed in the building''s local time. Use this for anything '
  'schedule-related; use ts for anything comparing across sites in different timezones.';


-- -----------------------------------------------------------------------------
-- v_setpoint_pair — measurement paired with the setpoint that governs it
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_setpoint_pair AS
SELECT
    e.equipment_id,
    e.name          AS equipment_name,
    e.equip_type,
    s.site_id,
    s.name          AS site_name,

    m.point_id      AS measured_point_id,
    COALESCE(m.display_name, m.niagara_history_name) AS measured_point_name,
    m.point_role    AS measured_role,
    m.unit          AS measured_unit,

    sp.point_id     AS setpoint_point_id,
    COALESCE(sp.display_name, sp.niagara_history_name) AS setpoint_point_name,
    sp.point_role   AS setpoint_role,
    sp.unit         AS setpoint_unit,

    (m.unit IS DISTINCT FROM sp.unit) AS unit_mismatch
FROM bas.point sp
JOIN bas.point_role spr ON spr.point_role   = sp.point_role
                       AND spr.setpoint_for IS NOT NULL
JOIN bas.point m        ON m.point_role     = spr.setpoint_for
                       AND m.equipment_id   = sp.equipment_id
JOIN bas.equipment e    ON e.equipment_id   = sp.equipment_id
JOIN bas.site s         ON s.site_id        = e.site_id
WHERE sp.is_active AND m.is_active;

COMMENT ON VIEW bas.v_setpoint_pair IS
'Every measured point matched to the setpoint that governs it, derived automatically from
point_role rather than configured per point. This is what makes "which zones never reached
setpoint last week" a single generic query instead of a per-building script.

Pairing requires both points to be assigned to the SAME equipment. A point with no
equipment_id will never appear here — which is the main practical reason to bother assigning
equipment.

Check unit_mismatch before comparing values. A setpoint in degC against a measurement in degF
produces a confident, wrong answer.';


-- -----------------------------------------------------------------------------
-- v_command_status_pair — command paired with its proof-of-operation feedback
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_command_status_pair AS
SELECT
    e.equipment_id,
    e.name        AS equipment_name,
    e.equip_type,
    s.site_id,
    s.name        AS site_name,

    c.point_id    AS command_point_id,
    COALESCE(c.display_name, c.niagara_history_name) AS command_point_name,
    c.point_role  AS command_role,

    stat.point_id AS status_point_id,
    COALESCE(stat.display_name, stat.niagara_history_name) AS status_point_name,
    stat.point_role AS status_role
FROM bas.point stat
JOIN bas.point_role sr ON sr.point_role = stat.point_role
                      AND sr.status_of  IS NOT NULL
JOIN bas.point c       ON c.point_role   = sr.status_of
                      AND c.equipment_id = stat.equipment_id
JOIN bas.equipment e   ON e.equipment_id = stat.equipment_id
JOIN bas.site s        ON s.site_id      = e.site_id
WHERE stat.is_active AND c.is_active;

COMMENT ON VIEW bas.v_command_status_pair IS
  'Every command point matched to the status point that proves whether it actually happened. '
  'Commanded-on-but-not-running is one of the most common and most expensive faults in a '
  'building, and it is invisible on an alarm screen. This view makes detecting it generic.';


-- -----------------------------------------------------------------------------
-- v_collection_health — operational, and deliberately cheap
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_collection_health AS
SELECT
    p.point_id,
    COALESCE(p.display_name, p.niagara_history_name) AS point_name,
    s.name  AS site_name,
    st.niagara_station_name,
    p.is_active,
    p.collection_interval_s,
    p.capacity,
    p.full_policy,
    p.roll_horizon_s,

    c.last_record_ts,
    c.last_run_at,
    c.last_status,
    c.consecutive_failures,
    c.last_error,

    EXTRACT(EPOCH FROM (now() - c.last_record_ts))::bigint AS seconds_since_last_record,

    CASE
        WHEN c.last_record_ts IS NULL           THEN 'never_collected'
        WHEN p.roll_horizon_s IS NULL           THEN 'roll_horizon_unknown'
        WHEN now() - c.last_record_ts
             > make_interval(secs => p.roll_horizon_s)      THEN 'data_lost'
        WHEN now() - c.last_record_ts
             > make_interval(secs => p.roll_horizon_s / 2.0) THEN 'at_risk'
        ELSE 'ok'
    END AS roll_risk
FROM bas.point p
JOIN      bas.station         st ON st.station_id = p.station_id
JOIN      bas.site            s  ON s.site_id     = st.site_id
LEFT JOIN bas.sync_checkpoint c  ON c.point_id    = p.point_id;

COMMENT ON VIEW bas.v_collection_health IS
'Per-point collection status. Cheap — reads checkpoints, never scans the readings table.

roll_risk is the important column. "data_lost" means more time has passed since our last
collected record than the station retains, so records have been overwritten and are gone
permanently. "roll_horizon_unknown" means capacity or collection_interval_s has not been
filled in from Workbench yet, so we cannot tell — treat that as a gap in our knowledge, not
as safety.';


-- -----------------------------------------------------------------------------
-- v_data_dictionary — the schema, annotated, in one query
-- -----------------------------------------------------------------------------

CREATE VIEW bas.v_data_dictionary AS
SELECT
    c.relname                                   AS object_name,
    CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' ELSE c.relkind::text END AS object_type,
    a.attname                                   AS column_name,
    format_type(a.atttypid, a.atttypmod)        AS data_type,
    NOT a.attnotnull                            AS is_nullable,
    col_description(c.oid, a.attnum)            AS column_description,
    obj_description(c.oid, 'pg_class')          AS object_description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'bas'
  AND c.relkind IN ('r','v')
ORDER BY c.relkind DESC, c.relname, a.attnum;

COMMENT ON VIEW bas.v_data_dictionary IS
  'The entire annotated schema in one query. Intended to be selected and pasted into an LLM '
  'prompt as context, so the model writes SQL against documented columns rather than '
  'guessing from names. Keep the COMMENT ON statements in the migrations current — they are '
  'not decoration, they are the model''s only description of what the data means.';
