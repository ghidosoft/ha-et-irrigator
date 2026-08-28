"""Constants for the ET Irrigator integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "et_irrigator"

# --- Top-level config keys -------------------------------------------------
CONF_ELEVATION: Final = "elevation"
CONF_SENSORS: Final = "sensors"
CONF_ZONES: Final = "zones"
CONF_ET_METHOD: Final = "et_method"

ET_METHOD_HOURLY: Final = "hourly"
ET_METHOD_DAILY: Final = "daily"
DEFAULT_ET_METHOD: Final = ET_METHOD_HOURLY

# --- Weather sensor keys ---------------------------------------------------
CONF_TEMPERATURE: Final = "temperature"
CONF_DEWPOINT: Final = "dewpoint"
CONF_WIND_SPEED: Final = "wind_speed"
CONF_WIND_MEASUREMENT_HEIGHT: Final = "wind_measurement_height"
CONF_SOLAR_RADIATION: Final = "solar_radiation"
CONF_RAIN: Final = "rain"

# --- Zone keys -------------------------------------------------------------
CONF_NAME: Final = "name"
CONF_AREA: Final = "area"
CONF_THROUGHPUT: Final = "throughput"
CONF_PRECIPITATION_RATE: Final = "precipitation_rate"
CONF_CROP_COEFFICIENT: Final = "crop_coefficient"
CONF_IRRIGATION_SENSOR: Final = "irrigation_sensor"
CONF_MAX_WINDOW_DAYS: Final = "max_window_days"
CONF_MAXIMUM_DEFICIT: Final = "maximum_deficit"
CONF_MAX_INFILTRATION_RATE: Final = "max_infiltration_rate"
CONF_MULTIPLIER: Final = "multiplier"
CONF_LEAD_TIME: Final = "lead_time"
CONF_MAXIMUM_DURATION: Final = "maximum_duration"

# --- Defaults --------------------------------------------------------------
DEFAULT_WIND_MEASUREMENT_HEIGHT: Final = 2.0  # metres
DEFAULT_WIND_SPEED: Final = 2.0  # m/s, FAO-56 default when wind data is missing
DEFAULT_CROP_COEFFICIENT: Final = 1.0
DEFAULT_MAX_WINDOW_DAYS: Final = 7
DEFAULT_MAXIMUM_DEFICIT: Final = 30.0  # mm (TAW: total available water in root zone)
# max_infiltration_rate has no default constant on purpose: the schema declares it
# vol.Optional with no `default=`, so an unset cap is *absent* from the validated
# config and read back as None via .get(). ZoneCalcConfig.max_infiltration_rate
# then defaults to None, which run_water_balance reads as "no cap".
DEFAULT_MULTIPLIER: Final = 1.0
DEFAULT_LEAD_TIME: Final = 0  # seconds
DEFAULT_MAXIMUM_DURATION: Final = -1  # seconds, -1 = no cap
ALBEDO: Final = 0.23  # FAO-56 reference grass
# Adjustment coefficient of the Hargreaves radiation formula (FAO-56 Eq. 50), which
# estimates Rs from the temperature range when the pyranometer has no usable
# reading. 0.16 is the 'interior' value; coastal sites use 0.19.
HARGREAVES_RADIATION_ADJ: Final = 0.16
# Consecutive hourly rows pinned to one value before the solar sensor is called
# stuck rather than merely steady — see statistics.py:_stuck_hours.
SOLAR_STUCK_MIN_HOURS: Final = 2

# --- Services --------------------------------------------------------------
SERVICE_RECALCULATE: Final = "recalculate"
SERVICE_RELOAD: Final = "reload"

# --- Hourly statistics export ----------------------------------------------
UNIT_MM: Final = "mm"

# Entity-id suffixes of the per-hour series. They are shared on purpose: the
# statistic_id we import into *is* the entity_id sensor.py builds from the same
# suffix, so the two can never drift apart.
SUFFIX_HOURLY_RAIN: Final = "hourly_rain"
SUFFIX_HOURLY_RUNOFF: Final = "hourly_runoff"
SUFFIX_HOURLY_DRAINAGE: Final = "hourly_drainage"
HOURLY_SUFFIXES: Final = (
    SUFFIX_HOURLY_RAIN,
    SUFFIX_HOURLY_RUNOFF,
    SUFFIX_HOURLY_DRAINAGE,
)

# --- Coordinator data attribute keys (sensor state attributes) -------------
ATTR_DEFICIT: Final = "deficit"
# Deprecated alias of -net_deficit, kept for Smart-Irrigation compatibility.
ATTR_DELTA: Final = "delta"
ATTR_NET_DEFICIT: Final = "net_deficit"
ATTR_EVAPOTRANSPIRATION: Final = "evapotranspiration"
ATTR_PRECIPITATION: Final = "precipitation"
ATTR_INFILTRATION: Final = "infiltration"
ATTR_DRAINAGE: Final = "drainage"
ATTR_RUNOFF: Final = "runoff"
ATTR_RAIN_LOST: Final = "rain_lost"  # drainage + runoff
ATTR_CAPPED: Final = "capped"
ATTR_SOIL_MOISTURE: Final = "soil_moisture"
ATTR_MAX_INFILTRATION_RATE: Final = "max_infiltration_rate"
ATTR_SIZE: Final = "size"
ATTR_THROUGHPUT: Final = "throughput"
ATTR_RATE: Final = "rate"  # effective application rate, mm/h
ATTR_CROP_COEFFICIENT: Final = "crop_coefficient"
ATTR_WINDOW_START: Final = "window_start"
ATTR_WINDOW_END: Final = "window_end"
ATTR_LAST_CALCULATED: Final = "last_calculated"
ATTR_NUMBER_OF_DATA_POINTS: Final = "number_of_data_points"
ATTR_MULTIPLIER: Final = "multiplier"
ATTR_LEAD_TIME: Final = "lead_time"
ATTR_MAXIMUM_DURATION: Final = "maximum_duration"
ATTR_MAXIMUM_DEFICIT: Final = "maximum_deficit"
ATTR_EXPLANATION: Final = "explanation"
# Newest hour the per-hour statistics export has covered, ISO-8601 or None. Lets
# the export be inspected without opening the recorder database.
ATTR_HOURLY_EXPORT_THROUGH: Final = "hourly_export_through"
