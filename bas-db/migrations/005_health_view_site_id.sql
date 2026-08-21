-- =============================================================================
-- 005_health_view_site_id.sql
--
-- Adds site_id and org_name to bas.v_collection_health.
--
-- Reason: dashboards need to filter by building, and filtering on a name is
-- fragile — names get renamed, and matching strings in a dashboard variable
-- means quoting rules that break the moment a building is called "St. Mary's".
-- Every other view already exposes site_id; this one was the odd one out.
--
-- Fixing forward with a new migration rather than editing 004, per the rule the
-- migration runner enforces.
-- =============================================================================

DROP VIEW IF EXISTS bas.v_collection_health;

CREATE VIEW bas.v_collection_health AS
SELECT
    p.point_id,
    COALESCE(p.display_name, p.niagara_history_name) AS point_name,
    p.point_role,
    p.unit,
    e.name  AS equipment_name,
    s.site_id,
    s.name  AS site_name,
    o.name  AS org_name,
    st.station_id,
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
        WHEN c.last_record_ts IS NULL THEN 'never_collected'
        WHEN p.roll_horizon_s IS NULL THEN 'roll_horizon_unknown'
        WHEN now() - c.last_record_ts
             > make_interval(secs => p.roll_horizon_s)       THEN 'data_lost'
        WHEN now() - c.last_record_ts
             > make_interval(secs => p.roll_horizon_s / 2.0) THEN 'at_risk'
        ELSE 'ok'
    END AS roll_risk
FROM bas.point p
JOIN      bas.station         st ON st.station_id   = p.station_id
JOIN      bas.site            s  ON s.site_id       = st.site_id
JOIN      bas.org             o  ON o.org_id        = s.org_id
LEFT JOIN bas.equipment       e  ON e.equipment_id  = p.equipment_id
LEFT JOIN bas.sync_checkpoint c  ON c.point_id      = p.point_id;

COMMENT ON VIEW bas.v_collection_health IS
'Per-point collection status. Cheap — reads checkpoints, never scans the readings table.

roll_risk is the important column. "data_lost" means more time has passed since our last
collected record than the station retains, so records have been overwritten and are gone
permanently. "roll_horizon_unknown" means capacity or collection_interval_s has not been
filled in from Workbench yet, so we cannot tell — treat that as a gap in our knowledge, not
as safety.

Filter by point_role to separate "a point is stale" from "a point that matters is stale",
and by site_id to scope to one building.';
