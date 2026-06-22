"""ET Irrigator — evapotranspiration-based irrigation from HA long-term statistics."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.typing import ConfigType

from .calc import ZoneCalcConfig
from .const import (
    CONF_AREA,
    CONF_CROP_COEFFICIENT,
    CONF_DEWPOINT,
    CONF_ELEVATION,
    CONF_ET_METHOD,
    CONF_IRRIGATION_SENSOR,
    CONF_LEAD_TIME,
    CONF_MAX_WINDOW_DAYS,
    CONF_MAXIMUM_DEFICIT,
    CONF_MAXIMUM_DURATION,
    CONF_MULTIPLIER,
    CONF_NAME,
    CONF_RAIN,
    CONF_SENSORS,
    CONF_SOLAR_RADIATION,
    CONF_TEMPERATURE,
    CONF_THROUGHPUT,
    CONF_WIND_MEASUREMENT_HEIGHT,
    CONF_WIND_SPEED,
    CONF_ZONES,
    DEFAULT_CROP_COEFFICIENT,
    DEFAULT_ET_METHOD,
    DEFAULT_LEAD_TIME,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_MAXIMUM_DEFICIT,
    DEFAULT_MAXIMUM_DURATION,
    DEFAULT_MULTIPLIER,
    DEFAULT_WIND_MEASUREMENT_HEIGHT,
    DOMAIN,
    ET_METHOD_DAILY,
    ET_METHOD_HOURLY,
    SERVICE_RECALCULATE,
)
from .coordinator import ETIrrigatorCoordinator, ZoneConfig

_LOGGER = logging.getLogger(__name__)

try:  # constant lives in a non-public module; pin defensively
    from homeassistant.components.recorder.const import (
        EVENT_RECORDER_HOURLY_STATISTICS_GENERATED,
    )
except ImportError:  # pragma: no cover
    EVENT_RECORDER_HOURLY_STATISTICS_GENERATED = "recorder_hourly_statistics_generated"

SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMPERATURE): cv.entity_id,
        vol.Optional(CONF_DEWPOINT): cv.entity_id,
        vol.Optional(CONF_WIND_SPEED): cv.entity_id,
        vol.Optional(CONF_SOLAR_RADIATION): cv.entity_id,
        vol.Optional(CONF_RAIN): cv.entity_id,
    }
)

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_AREA): vol.All(vol.Coerce(float), vol.Range(min=0.0001)),
        vol.Required(CONF_THROUGHPUT): vol.All(vol.Coerce(float), vol.Range(min=0.0001)),
        vol.Optional(
            CONF_CROP_COEFFICIENT, default=DEFAULT_CROP_COEFFICIENT
        ): vol.Coerce(float),
        vol.Optional(CONF_IRRIGATION_SENSOR): cv.entity_id,
        vol.Optional(
            CONF_MAX_WINDOW_DAYS, default=DEFAULT_MAX_WINDOW_DAYS
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional(
            CONF_MAXIMUM_DEFICIT, default=DEFAULT_MAXIMUM_DEFICIT
        ): vol.Coerce(float),
        vol.Optional(CONF_MULTIPLIER, default=DEFAULT_MULTIPLIER): vol.Coerce(float),
        vol.Optional(CONF_LEAD_TIME, default=DEFAULT_LEAD_TIME): vol.Coerce(int),
        vol.Optional(
            CONF_MAXIMUM_DURATION, default=DEFAULT_MAXIMUM_DURATION
        ): vol.Coerce(int),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ELEVATION): vol.Coerce(float),
                vol.Optional(
                    CONF_WIND_MEASUREMENT_HEIGHT,
                    default=DEFAULT_WIND_MEASUREMENT_HEIGHT,
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ET_METHOD, default=DEFAULT_ET_METHOD
                ): vol.In([ET_METHOD_HOURLY, ET_METHOD_DAILY]),
                vol.Required(CONF_SENSORS): SENSORS_SCHEMA,
                vol.Required(CONF_ZONES): vol.All(
                    cv.ensure_list, [ZONE_SCHEMA], vol.Length(min=1)
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up ET Irrigator from YAML."""
    conf = config[DOMAIN]

    latitude = hass.config.latitude
    elevation = conf.get(CONF_ELEVATION, hass.config.elevation)
    wind_height = conf[CONF_WIND_MEASUREMENT_HEIGHT]

    sensors_conf = conf[CONF_SENSORS]
    sensors = {
        "temperature": sensors_conf.get(CONF_TEMPERATURE),
        "dewpoint": sensors_conf.get(CONF_DEWPOINT),
        "wind_speed": sensors_conf.get(CONF_WIND_SPEED),
        "solar_radiation": sensors_conf.get(CONF_SOLAR_RADIATION),
        "rain": sensors_conf.get(CONF_RAIN),
    }

    zones: list[ZoneConfig] = []
    for zone_conf in conf[CONF_ZONES]:
        zones.append(
            ZoneConfig(
                name=zone_conf[CONF_NAME],
                irrigation_sensor=zone_conf.get(CONF_IRRIGATION_SENSOR),
                max_window_days=zone_conf[CONF_MAX_WINDOW_DAYS],
                calc=ZoneCalcConfig(
                    latitude=latitude,
                    elevation=elevation,
                    area=zone_conf[CONF_AREA],
                    throughput=zone_conf[CONF_THROUGHPUT],
                    crop_coefficient=zone_conf[CONF_CROP_COEFFICIENT],
                    maximum_deficit=zone_conf[CONF_MAXIMUM_DEFICIT],
                    multiplier=zone_conf[CONF_MULTIPLIER],
                    lead_time=zone_conf[CONF_LEAD_TIME],
                    maximum_duration=zone_conf[CONF_MAXIMUM_DURATION],
                ),
            )
        )

    coordinator = ETIrrigatorCoordinator(
        hass,
        sensors=sensors,
        wind_height=wind_height,
        zones=zones,
        et_method=conf[CONF_ET_METHOD],
        longitude=hass.config.longitude,
    )
    hass.data[DOMAIN] = coordinator

    await coordinator.async_refresh()

    # Recompute as soon as the recorder commits a new hour of statistics.
    async def _on_hourly_stats(_event) -> None:
        await coordinator.async_request_refresh()

    hass.bus.async_listen(
        EVENT_RECORDER_HOURLY_STATISTICS_GENERATED, _on_hourly_stats
    )

    async def _handle_recalculate(_call: ServiceCall) -> None:
        # Manual trigger: force an immediate (non-debounced) recompute.
        await coordinator.async_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_RECALCULATE, _handle_recalculate
    )

    hass.async_create_task(
        discovery.async_load_platform(hass, Platform.SENSOR, DOMAIN, {}, config)
    )
    return True
