-- =============================================================================
-- 002_vocabularies.sql
--
-- The semantic backbone. This is what turns a pile of numbers into something a
-- question can be asked of.
--
-- Concretely: without point_role, "what was the supply air temperature on AHU-3"
-- requires knowing that this particular building's integrator called it
-- AHU3_SAT, while the building next door calls it AHU-3/SupplyTemp and a third
-- calls it AHU_3_Sply_Air_T. With point_role it is `WHERE point_role =
-- 'supply_air_temp'` and it works everywhere.
--
-- Coverage is standard commercial HVAC. Roles are added by migration, not by
-- hand — if a building needs a role that is not here, that is a schema change
-- with a reason, not an ad-hoc string.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Equipment types
-- -----------------------------------------------------------------------------

INSERT INTO bas.equipment_type (equip_type, display_name, description, category) VALUES
  ('ahu',            'Air Handling Unit',        'Conditions and distributes air to a zone, floor, or building section.', 'air_side'),
  ('rtu',            'Rooftop Unit',             'Packaged rooftop air handler with integral heating and cooling.',       'air_side'),
  ('doas',           'Dedicated Outdoor Air System', 'Conditions outside air only, typically feeding terminal units.',    'air_side'),
  ('vav',            'VAV Box',                  'Variable air volume terminal regulating airflow into a zone.',          'terminal'),
  ('cav',            'CAV Box',                  'Constant air volume terminal.',                                          'terminal'),
  ('fcu',            'Fan Coil Unit',            'Local fan with a coil serving a single space.',                          'terminal'),
  ('unit_heater',    'Unit Heater',              'Local heating-only terminal.',                                           'terminal'),
  ('vrf_indoor',     'VRF Indoor Unit',          'Variable refrigerant flow indoor terminal.',                             'terminal'),
  ('vrf_outdoor',    'VRF Outdoor Unit',         'Variable refrigerant flow condensing unit.',                             'plant'),
  ('split_system',   'Split System',             'Split DX system with separate indoor and outdoor sections.',             'air_side'),
  ('crac',           'CRAC / CRAH Unit',         'Computer room air conditioning or air handling unit.',                   'air_side'),
  ('chiller',        'Chiller',                  'Produces chilled water for cooling.',                                    'plant'),
  ('boiler',         'Boiler',                   'Produces hot water or steam for heating.',                               'plant'),
  ('cooling_tower',  'Cooling Tower',            'Rejects condenser heat to atmosphere.',                                  'plant'),
  ('pump',           'Pump',                     'Circulates chilled, hot, or condenser water.',                           'water_side'),
  ('heat_exchanger', 'Heat Exchanger',           'Transfers heat between two fluid loops.',                                'water_side'),
  ('exhaust_fan',    'Exhaust Fan',              'Removes air from a space or system.',                                    'air_side'),
  ('plant_chw',      'Chilled Water Plant',      'The chilled water system as a whole: chillers, pumps, headers.',         'plant'),
  ('plant_hw',       'Hot Water Plant',          'The hot water system as a whole: boilers, pumps, headers.',              'plant'),
  ('zone',           'Zone',                     'A conditioned space. Not equipment, but points attach to it.',           'other'),
  ('meter_electric', 'Electric Meter',           'Measures electrical power or energy.',                                   'metering'),
  ('meter_gas',      'Gas Meter',                'Measures natural gas flow or volume.',                                   'metering'),
  ('meter_water',    'Water Meter',              'Measures water flow or volume.',                                         'metering'),
  ('weather',        'Weather Station',          'Outdoor conditions sensor package.',                                     'other'),
  ('other',          'Other',                    'Anything not otherwise classified.',                                     'other');


-- -----------------------------------------------------------------------------
-- Point roles
--
-- Links (setpoint_for / status_of) are applied after all rows exist, so that
-- insert order does not matter.
-- -----------------------------------------------------------------------------

INSERT INTO bas.point_role
  (point_role, display_name, description, measurement, typical_unit, is_setpoint, is_command, is_status)
VALUES
  -- Air temperatures --------------------------------------------------------
  ('supply_air_temp',        'Supply Air Temperature',        'Air temperature leaving the unit into the distribution system.', 'temperature', 'degF', false, false, false),
  ('supply_air_temp_sp',     'Supply Air Temperature Setpoint','Target supply air temperature.',                                'temperature', 'degF', true,  false, false),
  ('return_air_temp',        'Return Air Temperature',        'Air temperature returning from the space.',                      'temperature', 'degF', false, false, false),
  ('mixed_air_temp',         'Mixed Air Temperature',         'Air temperature after outside and return air mix, before coils.','temperature', 'degF', false, false, false),
  ('discharge_air_temp',     'Discharge Air Temperature',     'Air temperature leaving a terminal unit into the zone.',         'temperature', 'degF', false, false, false),
  ('discharge_air_temp_sp',  'Discharge Air Temperature Setpoint','Target discharge air temperature.',                          'temperature', 'degF', true,  false, false),
  ('outside_air_temp',       'Outside Air Temperature',       'Ambient outdoor dry bulb temperature.',                          'temperature', 'degF', false, false, false),
  ('preheat_air_temp',       'Preheat Air Temperature',       'Air temperature after a preheat coil.',                          'temperature', 'degF', false, false, false),

  -- Zone conditions ---------------------------------------------------------
  ('zone_temp',              'Zone Temperature',              'Measured space temperature.',                                    'temperature', 'degF', false, false, false),
  ('zone_temp_sp',           'Zone Temperature Setpoint',     'Target space temperature (single setpoint).',                    'temperature', 'degF', true,  false, false),
  ('zone_temp_cooling_sp',   'Zone Cooling Setpoint',         'Space temperature above which cooling is called.',               'temperature', 'degF', true,  false, false),
  ('zone_temp_heating_sp',   'Zone Heating Setpoint',         'Space temperature below which heating is called.',               'temperature', 'degF', true,  false, false),
  ('space_humidity',         'Space Relative Humidity',       'Measured relative humidity in the space.',                       'humidity',    'percent', false, false, false),
  ('space_humidity_sp',      'Space Humidity Setpoint',       'Target relative humidity.',                                      'humidity',    'percent', true,  false, false),
  ('return_air_humidity',    'Return Air Humidity',           'Relative humidity of return air.',                               'humidity',    'percent', false, false, false),
  ('outside_air_humidity',   'Outside Air Humidity',          'Ambient outdoor relative humidity.',                             'humidity',    'percent', false, false, false),
  ('dewpoint',              'Dewpoint Temperature',           'Dewpoint, measured or calculated.',                              'temperature', 'degF', false, false, false),
  ('space_co2',             'Space CO2',                      'Carbon dioxide concentration, a proxy for occupancy and ventilation adequacy.', 'concentration', 'ppm', false, false, false),
  ('space_co2_sp',          'Space CO2 Setpoint',             'CO2 level above which ventilation increases.',                   'concentration', 'ppm', true, false, false),

  -- Water temperatures ------------------------------------------------------
  ('chw_supply_temp',       'Chilled Water Supply Temperature','Chilled water leaving the plant.',                              'temperature', 'degF', false, false, false),
  ('chw_supply_temp_sp',    'Chilled Water Supply Setpoint',  'Target chilled water supply temperature.',                       'temperature', 'degF', true,  false, false),
  ('chw_return_temp',       'Chilled Water Return Temperature','Chilled water returning to the plant.',                         'temperature', 'degF', false, false, false),
  ('hw_supply_temp',        'Hot Water Supply Temperature',   'Hot water leaving the plant.',                                   'temperature', 'degF', false, false, false),
  ('hw_supply_temp_sp',     'Hot Water Supply Setpoint',      'Target hot water supply temperature.',                           'temperature', 'degF', true,  false, false),
  ('hw_return_temp',        'Hot Water Return Temperature',   'Hot water returning to the plant.',                              'temperature', 'degF', false, false, false),
  ('cw_supply_temp',        'Condenser Water Supply Temperature','Condenser water leaving the tower.',                           'temperature', 'degF', false, false, false),
  ('cw_return_temp',        'Condenser Water Return Temperature','Condenser water returning to the tower.',                      'temperature', 'degF', false, false, false),

  -- Pressure ----------------------------------------------------------------
  ('duct_static_pressure',    'Duct Static Pressure',         'Static pressure in the supply duct, the primary VAV fan control input.', 'pressure', 'inH2O', false, false, false),
  ('duct_static_pressure_sp', 'Duct Static Pressure Setpoint','Target duct static pressure.',                                   'pressure', 'inH2O', true,  false, false),
  ('building_static_pressure','Building Static Pressure',     'Pressure of the building relative to outside.',                  'pressure', 'inH2O', false, false, false),
  ('filter_dp',               'Filter Differential Pressure', 'Pressure drop across a filter bank. Rises as the filter loads.', 'pressure', 'inH2O', false, false, false),
  ('water_dp',                'Water Differential Pressure',  'Differential pressure across a water loop.',                     'pressure', 'psi',   false, false, false),
  ('suction_pressure',        'Suction Pressure',             'Refrigerant suction pressure.',                                  'pressure', 'psi',   false, false, false),
  ('discharge_pressure',      'Discharge Pressure',           'Refrigerant discharge pressure.',                                'pressure', 'psi',   false, false, false),

  -- Flow --------------------------------------------------------------------
  ('supply_air_flow',       'Supply Air Flow',                'Volumetric airflow supplied.',                                   'flow', 'cfm', false, false, false),
  ('supply_air_flow_sp',    'Supply Air Flow Setpoint',       'Target supply airflow.',                                          'flow', 'cfm', true,  false, false),
  ('outside_air_flow',      'Outside Air Flow',               'Volumetric outside air intake. Key to ventilation compliance.',   'flow', 'cfm', false, false, false),
  ('outside_air_flow_sp',   'Outside Air Flow Setpoint',      'Target outside airflow.',                                         'flow', 'cfm', true,  false, false),
  ('exhaust_air_flow',      'Exhaust Air Flow',               'Volumetric exhaust airflow.',                                     'flow', 'cfm', false, false, false),
  ('chw_flow',              'Chilled Water Flow',             'Chilled water volumetric flow.',                                  'flow', 'gpm', false, false, false),
  ('hw_flow',               'Hot Water Flow',                 'Hot water volumetric flow.',                                      'flow', 'gpm', false, false, false),

  -- Valve and damper positions ----------------------------------------------
  ('cooling_valve_cmd',     'Cooling Valve Command',          'Commanded position of the chilled water or DX cooling valve.',    'position', 'percent', false, true, false),
  ('heating_valve_cmd',     'Heating Valve Command',          'Commanded position of the heating valve.',                        'position', 'percent', false, true, false),
  ('reheat_valve_cmd',      'Reheat Valve Command',           'Commanded position of a terminal reheat valve.',                  'position', 'percent', false, true, false),
  ('oa_damper_cmd',         'Outside Air Damper Command',     'Commanded outside air damper position. The economizer output.',   'position', 'percent', false, true, false),
  ('ra_damper_cmd',         'Return Air Damper Command',      'Commanded return air damper position.',                           'position', 'percent', false, true, false),
  ('ea_damper_cmd',         'Exhaust Air Damper Command',     'Commanded exhaust/relief damper position.',                       'position', 'percent', false, true, false),
  ('vav_damper_cmd',        'VAV Damper Command',             'Commanded terminal box damper position.',                         'position', 'percent', false, true, false),
  ('bypass_damper_cmd',     'Bypass Damper Command',          'Commanded bypass damper position.',                               'position', 'percent', false, true, false),

  -- Fans --------------------------------------------------------------------
  ('supply_fan_cmd',        'Supply Fan Command',             'Start/stop command to the supply fan.',                           'status', NULL, false, true, false),
  ('supply_fan_status',     'Supply Fan Status',              'Proven running feedback for the supply fan.',                     'status', NULL, false, false, true),
  ('supply_fan_speed',      'Supply Fan Speed',               'Supply fan VFD speed.',                                           'speed',  'percent', false, true, false),
  ('return_fan_cmd',        'Return Fan Command',             'Start/stop command to the return fan.',                           'status', NULL, false, true, false),
  ('return_fan_status',     'Return Fan Status',              'Proven running feedback for the return fan.',                     'status', NULL, false, false, true),
  ('return_fan_speed',      'Return Fan Speed',               'Return fan VFD speed.',                                           'speed',  'percent', false, true, false),
  ('exhaust_fan_cmd',       'Exhaust Fan Command',            'Start/stop command to an exhaust fan.',                           'status', NULL, false, true, false),
  ('exhaust_fan_status',    'Exhaust Fan Status',             'Proven running feedback for an exhaust fan.',                     'status', NULL, false, false, true),

  -- Pumps and plant ---------------------------------------------------------
  ('pump_cmd',              'Pump Command',                   'Start/stop command to a pump.',                                   'status', NULL, false, true, false),
  ('pump_status',           'Pump Status',                    'Proven running feedback for a pump.',                             'status', NULL, false, false, true),
  ('pump_speed',            'Pump Speed',                     'Pump VFD speed.',                                                 'speed',  'percent', false, true, false),
  ('chiller_cmd',           'Chiller Command',                'Enable command to a chiller.',                                    'status', NULL, false, true, false),
  ('chiller_status',        'Chiller Status',                 'Running feedback from a chiller.',                                'status', NULL, false, false, true),
  ('boiler_cmd',            'Boiler Command',                 'Enable command to a boiler.',                                     'status', NULL, false, true, false),
  ('boiler_status',         'Boiler Status',                  'Running feedback from a boiler.',                                 'status', NULL, false, false, true),
  ('compressor_cmd',        'Compressor Command',             'Enable command to a compressor.',                                 'status', NULL, false, true, false),
  ('compressor_status',     'Compressor Status',              'Running feedback from a compressor.',                             'status', NULL, false, false, true),
  ('tower_fan_cmd',         'Cooling Tower Fan Command',      'Start/stop command to a cooling tower fan.',                       'status', NULL, false, true, false),
  ('tower_fan_status',      'Cooling Tower Fan Status',       'Proven running feedback for a cooling tower fan.',                 'status', NULL, false, false, true),
  ('cooling_stage_cmd',     'Cooling Stage Command',          'Number or state of cooling stages commanded.',                     'status', NULL, false, true, false),
  ('heating_stage_cmd',     'Heating Stage Command',          'Number or state of heating stages commanded.',                     'status', NULL, false, true, false),

  -- Energy and electrical ---------------------------------------------------
  ('power_kw',              'Real Power',                     'Instantaneous real power draw.',                                  'power',  'kW',   false, false, false),
  ('demand_kw',             'Peak Demand',                    'Demand value used for utility billing.',                          'power',  'kW',   false, false, false),
  ('energy_kwh',            'Energy Consumption',             'Cumulative electrical energy. Note this is usually a running total, not an interval value.', 'energy', 'kWh', false, false, false),
  ('current_amps',          'Current',                        'Electrical current.',                                             'current','A',    false, false, false),
  ('voltage',               'Voltage',                        'Electrical potential.',                                           'voltage','V',    false, false, false),
  ('power_factor',          'Power Factor',                   'Ratio of real to apparent power.',                                'ratio',  NULL,   false, false, false),
  ('gas_volume',            'Gas Volume',                     'Cumulative natural gas volume.',                                  'volume', 'ccf',  false, false, false),
  ('water_volume',          'Water Volume',                   'Cumulative water volume.',                                        'volume', 'gal',  false, false, false),

  -- Operational state -------------------------------------------------------
  ('occupancy_status',      'Occupancy Status',               'Whether the space or system is currently in occupied mode.',      'status', NULL, false, false, true),
  ('occupancy_cmd',         'Occupancy Command',              'Commanded occupancy mode, usually from a schedule.',              'status', NULL, false, true, false),
  ('occupancy_sensor',      'Occupancy Sensor',               'Physical presence detection in a space.',                         'status', NULL, false, false, true),
  ('occupancy_override',    'Occupancy Override',             'Manual after-hours override request.',                            'status', NULL, false, false, true),
  ('system_mode',           'System Mode',                    'Operating mode, e.g. heating / cooling / off / economizer.',      'mode',   NULL, false, false, true),
  ('alarm_status',          'Alarm Status',                   'Alarm condition reported by equipment.',                          'status', NULL, false, false, true),
  ('fault_status',          'Fault Status',                   'Fault condition reported by equipment.',                          'status', NULL, false, false, true),
  ('filter_status',         'Filter Status',                  'Dirty-filter indication.',                                        'status', NULL, false, false, true),
  ('freeze_status',         'Freezestat Status',              'Freeze protection trip.',                                         'status', NULL, false, false, true),
  ('smoke_status',          'Smoke Detector Status',          'Duct or area smoke detection.',                                   'status', NULL, false, false, true),
  ('run_hours',             'Run Hours',                      'Cumulative equipment runtime.',                                   'time',   'h',  false, false, false),
  ('start_count',           'Start Count',                    'Cumulative number of equipment starts.',                          'count',  NULL, false, false, false),

  -- Escape hatch ------------------------------------------------------------
  ('unclassified',          'Unclassified',                   'Deliberately not yet classified. Distinct from NULL, which means nobody has looked. Use this to mark a point as reviewed-but-not-mappable.', NULL, NULL, false, false, false);


-- -----------------------------------------------------------------------------
-- Semantic links between roles.
--
-- These two updates are what make generic fault rules possible. With them, a
-- rule like "commanded on but not running" or "never reached setpoint" can be
-- written once and applied to every building, rather than once per point pair.
-- -----------------------------------------------------------------------------

UPDATE bas.point_role SET setpoint_for = v.target
FROM (VALUES
    ('supply_air_temp_sp',     'supply_air_temp'),
    ('discharge_air_temp_sp',  'discharge_air_temp'),
    ('zone_temp_sp',           'zone_temp'),
    ('zone_temp_cooling_sp',   'zone_temp'),
    ('zone_temp_heating_sp',   'zone_temp'),
    ('space_humidity_sp',      'space_humidity'),
    ('space_co2_sp',           'space_co2'),
    ('chw_supply_temp_sp',     'chw_supply_temp'),
    ('hw_supply_temp_sp',      'hw_supply_temp'),
    ('duct_static_pressure_sp','duct_static_pressure'),
    ('supply_air_flow_sp',     'supply_air_flow'),
    ('outside_air_flow_sp',    'outside_air_flow')
) AS v(role, target)
WHERE bas.point_role.point_role = v.role;

UPDATE bas.point_role SET status_of = v.target
FROM (VALUES
    ('supply_fan_status',  'supply_fan_cmd'),
    ('return_fan_status',  'return_fan_cmd'),
    ('exhaust_fan_status', 'exhaust_fan_cmd'),
    ('pump_status',        'pump_cmd'),
    ('chiller_status',     'chiller_cmd'),
    ('boiler_status',      'boiler_cmd'),
    ('compressor_status',  'compressor_cmd'),
    ('tower_fan_status',   'tower_fan_cmd')
) AS v(role, target)
WHERE bas.point_role.point_role = v.role;
