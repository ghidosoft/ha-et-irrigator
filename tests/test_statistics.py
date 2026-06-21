"""Verify the statistics layer converts sensor units to what calc.py expects."""

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.et_irrigator.const import DEFAULT_WIND_SPEED
from custom_components.et_irrigator.statistics import (
    async_fetch_statistics,
    build_days,
)


def _hours(value_key, value, *, n=3, day=15):
    """Build n hourly statistic rows on a fixed day for one sensor."""
    base = datetime(2026, 6, day, 10, 0, tzinfo=timezone.utc)
    return [
        {"start": base + timedelta(hours=i), value_key: value, "min": value, "max": value}
        for i in range(n)
    ]


def _all_sensors(**overrides):
    sensors = {
        "temperature": "sensor.t",
        "dewpoint": None,
        "wind_speed": None,
        "solar_radiation": None,
        "rain": None,
    }
    sensors.update(overrides)
    return sensors


def test_wind_defaults_to_fao56_when_missing():
    """No wind sensor -> FAO-56 2 m/s default, not 0 (OBS 5)."""
    stats = {"sensor.t": _hours("mean", 22.0)}
    days = build_days(stats, _all_sensors(), wind_height=2.0)
    assert days and days[0].wind_speed == DEFAULT_WIND_SPEED


def test_rain_measurement_warns_and_zeroes_precip(caplog):
    """A measurement rain sensor yields no `change` -> precip 0 + warning (BUG 1)."""
    stats = {
        "sensor.t": _hours("mean", 22.0),
        # measurement rain: rows carry no 'change' key at all
        "sensor.rain": _hours("mean", 1.0),
    }
    with caplog.at_level(logging.WARNING):
        days = build_days(stats, _all_sensors(rain="sensor.rain"), wind_height=2.0)
    assert days and days[0].precipitation_mm == 0.0
    assert "total_increasing" in caplog.text


def test_rain_total_increasing_sums_change():
    """A total_increasing rain sensor contributes its per-hour `change`."""
    stats = {
        "sensor.t": _hours("mean", 22.0),
        "sensor.rain": _hours("change", 0.5),
    }
    days = build_days(stats, _all_sensors(rain="sensor.rain"), wind_height=2.0)
    assert days and abs(days[0].precipitation_mm - 1.5) < 1e-9  # 3 hours * 0.5

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
