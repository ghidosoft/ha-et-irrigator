"""Verify the statistics layer converts sensor units to what calc.py expects."""

from datetime import timedelta

from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.et_irrigator.statistics import (
    async_fetch_statistics,
    build_days,
)

WIND_ID = "sensor.test_wind"
TEMP_ID = "sensor.test_temp"


async def _import_hourly(hass, statistic_id, unit, value, hours):
    """Insert `hours` of hourly statistics ending now."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=hours
    )
    stats = [
        {
            "start": start + timedelta(hours=i),
            "mean": value,
            "min": value,
            "max": value,
        }
        for i in range(hours)
    ]
    metadata = {
        "has_mean": True,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    async_import_statistics(hass, metadata, stats)
    await async_wait_recording_done(hass)


async def test_wind_kmh_is_converted_to_ms(recorder_mock, hass):
    # 36 km/h must come back as 10 m/s; temp 20 °C unchanged.
    await _import_hourly(hass, WIND_ID, "km/h", 36.0, hours=6)
    await _import_hourly(hass, TEMP_ID, "°C", 20.0, hours=6)

    start = dt_util.utcnow() - timedelta(hours=7)
    now = dt_util.utcnow()
    stats = await async_fetch_statistics(hass, {WIND_ID, TEMP_ID}, start, now)

    sensors = {
        "temperature": TEMP_ID,
        "wind_speed": WIND_ID,
        "dewpoint": None,
        "humidity": None,
        "solar_radiation": None,
        "rain": None,
    }
    days = build_days(stats, sensors, wind_height=10.0)

    assert days, "expected at least one aggregated day"
    assert abs(days[0].wind_speed - 10.0) < 1e-6  # 36 km/h -> 10 m/s
    assert abs(days[0].t_min - 20.0) < 1e-6
