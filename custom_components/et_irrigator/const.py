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
CONF_MULTIPLIER: Final = "multiplier"
CONF_LEAD_TIME: Final = "lead_time"
CONF_MAXIMUM_DURATION: Final = "maximum_duration"

# --- Defaults --------------------------------------------------------------
DEFAULT_WIND_MEASUREMENT_HEIGHT: Final = 2.0  # metres
DEFAULT_WIND_SPEED: Final = 2.0  # m/s, FAO-56 default when wind data is missing
DEFAULT_CROP_COEFFICIENT: Final = 1.0
DEFAULT_MAX_WINDOW_DAYS: Final = 7
DEFAULT_MAXIMUM_DEFICIT: Final = 30.0  # mm (field capacity cap)
DEFAULT_MULTIPLIER: Final = 1.0
DEFAULT_LEAD_TIME: Final = 0  # seconds
DEFAULT_MAXIMUM_DURATION: Final = -1  # seconds, -1 = no cap
ALBEDO: Final = 0.23  # FAO-56 reference grass

# --- Services --------------------------------------------------------------
SERVICE_RECALCULATE: Final = "recalculate"
SERVICE_RELOAD: Final = "reload"

# --- Coordinator data attribute keys (sensor state attributes) -------------
ATTR_DEFICIT: Final = "deficit"
ATTR_DELTA: Final = "delta"
ATTR_EVAPOTRANSPIRATION: Final = "evapotranspiration"
ATTR_PRECIPITATION: Final = "precipitation"
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
