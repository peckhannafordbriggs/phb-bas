-- =============================================================================
-- 001_core_schema.sql
--
-- The BAS data model.
--
-- Two layers, deliberately separated:
--   * METADATA  — rich, mutable, small. What the numbers mean.
--   * READINGS  — thin, immutable, large. The numbers.
--
-- Three invariants that are expensive or impossible to fix later, so they are
-- enforced structurally rather than by convention:
--
--   1. Point identity is a surrogate key, never a name. Niagara names change;
--      when they do we get a new point row, not corrupted history.
--   2. All timestamps are timestamptz, stored UTC. Local time is a display
--      concern, derived from the site's IANA zone. There is no way to unwind a
--      DST bug after the fact.
--   3. The readings table carries no names, units, or equipment. Denormalizing
--      those multiplies storage ~5x and turns a rename into a billion-row
--      rewrite. Join to bas.point instead.
--
-- Everything lives in the `bas` schema so this can later be attached alongside
-- other schemas without any chance of a table name colliding.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS bas;

COMMENT ON SCHEMA bas IS
  'Building automation system data: trend history extracted from Niagara 4 stations, '
  'plus the semantic metadata needed to interpret it.';


-- =============================================================================
-- VOCABULARIES
--
-- These are tables rather than Postgres enums on purpose. An analyst — or an
-- LLM writing SQL — can SELECT from them to discover what values exist and what
-- they mean. An enum is invisible from inside a query and painful to extend.
-- =============================================================================

CREATE TABLE bas.equipment_type (
    equip_type    text PRIMARY KEY,
    display_name  text NOT NULL,
    description   text NOT NULL,
    category      text NOT NULL
);

COMMENT ON TABLE bas.equipment_type IS
  'Controlled vocabulary of equipment kinds. Lets questions like "compare all AHUs" '
  'work without string-matching on names.';
COMMENT ON COLUMN bas.equipment_type.category IS
  'Coarse grouping: air_side, water_side, plant, terminal, metering, other.';


CREATE TABLE bas.point_role (
    point_role    text PRIMARY KEY,
    display_name  text NOT NULL,
    description   text NOT NULL,
    measurement   text,
    typical_unit  text,
    is_setpoint   boolean NOT NULL DEFAULT false,
    is_command    boolean NOT NULL DEFAULT false,
    is_status     boolean NOT NULL DEFAULT false,

    -- Semantic links between roles. These are what let generic questions work.
    --
    -- setpoint_for: on 'supply_air_temp_sp', this is 'supply_air_temp'. That is
    --   how "which units never reached setpoint" is answerable across every
    --   building without hardcoding point pairs.
    -- status_of: on 'supply_fan_status', this is 'supply_fan_cmd'. That is how
    --   "commanded on but not running" — a real, common, expensive fault —
    --   becomes a generic query.
    setpoint_for  text REFERENCES bas.point_role(point_role),
    status_of     text REFERENCES bas.point_role(point_role)
);

COMMENT ON TABLE bas.point_role IS
  'Controlled vocabulary describing WHAT KIND of measurement a point is. This is the '
  'highest-leverage table in the schema: without it every analytical question degenerates '
  'into string-matching against whatever naming convention a given integrator happened to '
  'use. With it, "compare supply air temperature across all AHUs" works across buildings, '
  'naming schemes, and vendors.';
COMMENT ON COLUMN bas.point_role.measurement IS
  'Physical quantity: temperature, humidity, pressure, flow, position, power, energy, '
  'speed, status, mode, time. Null for roles that are not a physical measurement.';
COMMENT ON COLUMN bas.point_role.setpoint_for IS
  'If this role is a setpoint, the role it targets. Join point-to-point on matching '
  'equipment_id to pair a setpoint with its measurement.';
COMMENT ON COLUMN bas.point_role.status_of IS
  'If this role is a feedback/status, the command role it reports on. A command that is '
  'on while its status is off is a fault.';


-- =============================================================================
-- HIERARCHY
--
--   org  ->  site  ->  station  ->  point  ->  reading
--                  \->  equipment  ->/
-- =============================================================================

CREATE TABLE bas.org (
    org_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE bas.org IS
  'Portfolio owner — the customer or business unit that owns a set of buildings. One row '
  'today; the column exists so that multi-customer data never has to be retrofitted.';


CREATE TABLE bas.site (
    site_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      bigint NOT NULL REFERENCES bas.org(org_id),
    name        text NOT NULL,
    address     text,
    timezone    text NOT NULL,
    area_sqft   integer,
    attributes  jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

COMMENT ON TABLE bas.site IS 'A building.';
COMMENT ON COLUMN bas.site.timezone IS
  'IANA timezone name, e.g. America/New_York. DISPLAY ONLY — every stored timestamp is '
  'UTC. This is what converts UTC back to "what time was it in the building", which is '
  'the only frame occupancy schedules and business hours make sense in. Must be a value '
  'from pg_timezone_names.';
COMMENT ON COLUMN bas.site.attributes IS
  'Open-ended site metadata (building type, year built, floors). Deliberately loose — this '
  'is the layer most likely to need fields we have not thought of.';


CREATE TABLE bas.station (
    station_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id               bigint NOT NULL REFERENCES bas.site(site_id),
    niagara_station_name  text NOT NULL,
    base_url              text,
    host_id               text,
    model                 text,
    niagara_version       text,
    parent_station_id     bigint REFERENCES bas.station(station_id),
    is_active             boolean NOT NULL DEFAULT true,
    notes                 text,
    first_seen_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_at          timestamptz,
    UNIQUE (site_id, niagara_station_name)
);

COMMENT ON TABLE bas.station IS
  'A running Niagara station — a JACE, or a Supervisor. One row per controller.';
COMMENT ON COLUMN bas.station.niagara_station_name IS
  'The station name EXACTLY as Niagara spells it, including capitalization. This appears '
  'literally in every oBIX URL. Get the case wrong and every request 404s.';
COMMENT ON COLUMN bas.station.parent_station_id IS
  'The Supervisor this station reports to, if any. NULL for a standalone JACE. This single '
  'nullable self-reference is how a Supervisor stays optional forever — introducing one '
  'later needs no schema change at all.';


CREATE TABLE bas.equipment (
    equipment_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id              bigint NOT NULL REFERENCES bas.site(site_id),
    name                 text NOT NULL,
    equip_type           text REFERENCES bas.equipment_type(equip_type),
    parent_equipment_id  bigint REFERENCES bas.equipment(equipment_id),
    attributes           jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, name),
    CONSTRAINT equipment_not_own_parent CHECK (parent_equipment_id IS DISTINCT FROM equipment_id)
);

COMMENT ON TABLE bas.equipment IS
  'A physical piece of equipment: AHU-3, VAV-204, Chiller-1. Expect this to be wrong at '
  'first and cheap to correct — nothing depends on its values, only on its identity.';
COMMENT ON COLUMN bas.equipment.parent_equipment_id IS
  'Equipment served by this one, e.g. a VAV box under the AHU that feeds it. Enables '
  '"show me everything downstream of AHU-3".';


-- =============================================================================
-- POINT — the crux table
-- =============================================================================

CREATE TABLE bas.point (
    point_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id             bigint NOT NULL REFERENCES bas.station(station_id),
    equipment_id           bigint REFERENCES bas.equipment(equipment_id),

    -- Identity as Niagara knows it.
    niagara_history_name   text NOT NULL,
    niagara_history_ord    text,

    display_name           text,
    point_role             text REFERENCES bas.point_role(point_role),

    -- Captured at ingest, from the oBIX #RecordDef prototype.
    unit                   text,
    data_type              text NOT NULL DEFAULT 'unknown'
                             CHECK (data_type IN ('real','int','bool','str','enum','abstime','unknown')),
    source_timezone        text,

    -- From Workbench / BQL. NOT obtainable over oBIX.
    collection_interval_s  integer CHECK (collection_interval_s IS NULL OR collection_interval_s > 0),
    capacity               integer CHECK (capacity IS NULL OR capacity > 0),
    full_policy            text CHECK (full_policy IN ('roll','stop')),

    roll_horizon_s         integer GENERATED ALWAYS AS (capacity * collection_interval_s) STORED,

    tags                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes                  text,

    is_active              boolean NOT NULL DEFAULT true,
    first_seen_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at           timestamptz,

    UNIQUE (station_id, niagara_history_name)
);

COMMENT ON TABLE bas.point IS
  'One trended value in the building. The natural key is (station_id, niagara_history_name); '
  'everything else references the surrogate point_id. A point renamed in Niagara therefore '
  'appears as a NEW point rather than silently rewriting the meaning of existing history — '
  'that is deliberate, and the old row should be marked inactive after a human looks at it.';
COMMENT ON COLUMN bas.point.niagara_history_name IS
  'The history name EXACTLY as Niagara returns it, INCLUDING $-hex escapes ($20 = space, '
  '$2d = dash). This string goes into the oBIX URL verbatim. Never store the pretty '
  'decoded form here — decoding and re-encoding is not reliably round-trippable and '
  'produces 404s that look exactly like missing points.';
COMMENT ON COLUMN bas.point.point_role IS
  'What KIND of measurement this is. The single most important field for analysis. A point '
  'with no role is invisible to every cross-equipment comparison and every generic fault '
  'rule — so unclassified points should be an explicit, visible backlog.';
COMMENT ON COLUMN bas.point.unit IS
  'Engineering unit as reported by oBIX, with the "obix:units/" prefix stripped. Captured at '
  'ingest because recovering it for historical data afterwards ranges from painful to '
  'impossible — and comparing 55 degF against 12.8 degC silently produces a confident '
  'wrong answer.';
COMMENT ON COLUMN bas.point.collection_interval_s IS
  'How often Niagara logs this point, in seconds. From the history extension in Workbench, '
  'or BQL. NOT available over oBIX.';
COMMENT ON COLUMN bas.point.capacity IS
  'Maximum records the station retains for this history before the Full Policy applies. '
  'Niagara defaults to 500. From Workbench/BQL only.';
COMMENT ON COLUMN bas.point.full_policy IS
  '"roll" = oldest records are overwritten (Niagara default, and silent). "stop" = logging '
  'halts when full.';
COMMENT ON COLUMN bas.point.roll_horizon_s IS
  'capacity * collection_interval_s: how far back this history reaches before the station '
  'destroys data. Computed, not stored by hand. The collector must poll far more often than '
  'this — polling slower loses data permanently, with no error and no gap marker anywhere.';


-- =============================================================================
-- READINGS — deliberately narrow
-- =============================================================================

CREATE TABLE bas.reading (
    point_id    bigint      NOT NULL REFERENCES bas.point(point_id) ON DELETE CASCADE,
    ts          timestamptz NOT NULL,
    value_num   double precision,
    value_bool  boolean,
    value_str   text,
    status      text,
    PRIMARY KEY (point_id, ts),
    CONSTRAINT reading_at_most_one_value CHECK (
        (value_num  IS NOT NULL)::int
      + (value_bool IS NOT NULL)::int
      + (value_str  IS NOT NULL)::int <= 1
    )
);

COMMENT ON TABLE bas.reading IS
  'One trend record. Append-only in practice: never UPDATE, never DELETE. Derived values '
  'and rollups belong in views or separate tables so that raw data stays reproducible when '
  'an answer is later questioned.';
COMMENT ON COLUMN bas.reading.ts IS
  'Instant the value was recorded, UTC. Convert to bas.site.timezone for display or for any '
  'question about occupancy, business hours, or "last Tuesday".';
COMMENT ON CONSTRAINT reading_at_most_one_value ON bas.reading IS
  'At most one typed value column is populated. ZERO populated columns is valid and '
  'meaningful: it is a record the station returned as null — a sensor fault or a real gap. '
  'That is different from no row at all, which means we never collected it.';
COMMENT ON COLUMN bas.reading.status IS
  'Niagara status flags as reported, e.g. "{down}" or "{overridden}". A value present with '
  'an override flag is not the same as a value the building actually produced.';

-- The primary key (point_id, ts) already serves the dominant access pattern:
-- "this point, over this window". It is also the idempotency guarantee and the
-- dedup mechanism — re-running a collection cannot double-insert.
--
-- This BRIN index serves the other pattern: "everything, over this window".
-- BRIN is tiny (kilobytes, not gigabytes) and works extremely well here because
-- readings are appended in roughly timestamp order, so physical page order
-- tracks time order.
CREATE INDEX reading_ts_brin ON bas.reading USING brin (ts) WITH (pages_per_range = 32);


-- =============================================================================
-- SEMANTIC LINKS BETWEEN POINTS
-- =============================================================================

CREATE TABLE bas.point_link (
    from_point_id  bigint NOT NULL REFERENCES bas.point(point_id) ON DELETE CASCADE,
    to_point_id    bigint NOT NULL REFERENCES bas.point(point_id) ON DELETE CASCADE,
    link_type      text   NOT NULL CHECK (link_type IN
                       ('setpoint_for','status_of','feedback_for','serves','measures_same')),
    confidence     text   NOT NULL DEFAULT 'manual'
                       CHECK (confidence IN ('manual','inferred')),
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_point_id, to_point_id, link_type),
    CONSTRAINT point_link_not_self CHECK (from_point_id <> to_point_id)
);

COMMENT ON TABLE bas.point_link IS
  'Explicit relationships between specific points, for cases the role vocabulary cannot '
  'infer — an AHU with two supply temperature sensors, a setpoint shared across zones, a '
  'meter that submeters another. Prefer inferring from point_role + equipment_id where '
  'possible; use this where that would be wrong.';
COMMENT ON COLUMN bas.point_link.confidence IS
  '"manual" = a human asserted it. "inferred" = derived by a script from naming or '
  'structure. Analysis that matters should be able to tell the difference.';


-- =============================================================================
-- OPERATIONAL TABLES
--
-- These are not analytics. They are the difference between "the data looks
-- wrong" and "I can see exactly which run went wrong, when, and why."
-- =============================================================================

CREATE TABLE bas.sync_checkpoint (
    point_id              bigint PRIMARY KEY REFERENCES bas.point(point_id) ON DELETE CASCADE,
    last_record_ts        timestamptz,
    last_run_at           timestamptz,
    last_status           text NOT NULL DEFAULT 'never_run'
                            CHECK (last_status IN ('never_run','ok','error','skipped')),
    consecutive_failures  integer NOT NULL DEFAULT 0,
    last_error            text
);

COMMENT ON TABLE bas.sync_checkpoint IS
  'Per-point high-water mark. The reason the collector self-heals: after a network drop, a '
  'station reboot, or a database outage, the next run resumes from here with no human '
  'involvement and no duplicates.';
COMMENT ON COLUMN bas.sync_checkpoint.last_record_ts IS
  'Timestamp of the newest record successfully COMMITTED for this point. Advanced only '
  'after a successful write — never before. Deliberately not derived from MAX(ts) on the '
  'readings table, because that cannot express "we tried and failed", cannot support '
  'backfill running independently of forward collection, and cannot distinguish a point '
  'that is idle from one that is broken.';


CREATE TABLE bas.ingest_run (
    run_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id         bigint REFERENCES bas.station(station_id),
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    status             text NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running','ok','partial','failed')),
    window_start       timestamptz,
    window_end         timestamptz,
    points_attempted   integer NOT NULL DEFAULT 0,
    points_succeeded   integer NOT NULL DEFAULT 0,
    records_written    integer NOT NULL DEFAULT 0,
    errors             jsonb   NOT NULL DEFAULT '[]'::jsonb,
    collector_version  text,
    collector_host     text
);

COMMENT ON TABLE bas.ingest_run IS
  'One row per collector execution. The audit trail — worth having from the very first run, '
  'because the alternative is discovering a gap months later with no way to explain it.';


CREATE TABLE bas.data_gap (
    gap_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    point_id     bigint NOT NULL REFERENCES bas.point(point_id) ON DELETE CASCADE,
    gap_start    timestamptz NOT NULL,
    gap_end      timestamptz NOT NULL,
    detected_at  timestamptz NOT NULL DEFAULT now(),
    cause        text NOT NULL CHECK (cause IN
                   ('roll_overwrite','collector_down','station_unreachable',
                    'point_added_later','station_clock_change','unknown')),
    notes        text,
    CONSTRAINT data_gap_ordered CHECK (gap_end >= gap_start)
);

COMMENT ON TABLE bas.data_gap IS
  'Periods we know we are missing, and why. Recording gaps explicitly is far better than '
  'silently having holes: it lets analysis distinguish "the equipment was off" from "we '
  'were not looking". An AI answering questions about this data needs that distinction to '
  'avoid confidently reporting a shutdown that never happened.';
COMMENT ON COLUMN bas.data_gap.cause IS
  '"roll_overwrite" is the unrecoverable one — the station destroyed the data before we '
  'collected it. Every occurrence is a signal that the poll cadence is wrong for that point.';


-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX point_station_idx     ON bas.point (station_id);
CREATE INDEX point_equipment_idx   ON bas.point (equipment_id) WHERE equipment_id IS NOT NULL;
CREATE INDEX point_role_idx        ON bas.point (point_role)   WHERE point_role IS NOT NULL;
CREATE INDEX point_active_idx      ON bas.point (is_active)    WHERE is_active;
CREATE INDEX equipment_site_idx    ON bas.equipment (site_id);
CREATE INDEX equipment_type_idx    ON bas.equipment (equip_type) WHERE equip_type IS NOT NULL;
CREATE INDEX equipment_parent_idx  ON bas.equipment (parent_equipment_id) WHERE parent_equipment_id IS NOT NULL;
CREATE INDEX station_site_idx      ON bas.station (site_id);
CREATE INDEX ingest_run_started_idx ON bas.ingest_run (started_at DESC);
CREATE INDEX data_gap_point_idx    ON bas.data_gap (point_id, gap_start);
CREATE INDEX point_link_to_idx     ON bas.point_link (to_point_id);
