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
    build_hours,
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


# --- build_hours (hourly method) -------------------------------------------

def _aligned_rows(value_key, value, *, n=3):
    base = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    return [{"start": base + timedelta(hours=i), value_key: value} for i in range(n)]


def test_build_hours_aligns_channels_and_converts_solar():
    stats = {
        "sensor.t": _aligned_rows("mean", 25.0),
        "sensor.w": _aligned_rows("mean", 3.0),
        "sensor.s": _aligned_rows("mean", 800.0),  # W/m2
        "sensor.d": _aligned_rows("mean", 16.0),
        "sensor.r": _aligned_rows("change", 0.2),
    }
    sensors = {
        "temperature": "sensor.t",
        "wind_speed": "sensor.w",
        "solar_radiation": "sensor.s",
        "dewpoint": "sensor.d",
        "rain": "sensor.r",
    }
    hours = build_hours(stats, sensors, wind_height=10.0, longitude=10.0)

    assert len(hours) == 3
    h = hours[0]
    assert h.t == 25.0
    assert h.wind_speed == 3.0
    assert h.dewpoint == 16.0
    assert abs(h.solar_rad_mj - 800.0 * 3600 / 1_000_000.0) < 1e-9  # -> 2.88 MJ/m²/h
    assert h.precipitation_mm == 0.2
    assert h.wind_height == 10.0
    # 12:30 UTC midpoint, lon +10° -> solar time ~13.2h (+ small seasonal term)
    assert 12.8 < h.solar_time_hours < 13.6


def test_build_hours_wind_defaults_when_missing():
    stats = {"sensor.t": _aligned_rows("mean", 22.0)}
    sensors = {
        "temperature": "sensor.t",
        "wind_speed": None,
        "solar_radiation": None,
        "dewpoint": None,
        "rain": None,
    }
    hours = build_hours(stats, sensors, wind_height=2.0, longitude=10.0)
    assert hours and all(h.wind_speed == DEFAULT_WIND_SPEED for h in hours)


def test_build_hours_keeps_rain_only_hours():
    """A recorder gap in temperature must not swallow that hour's rain.

    The water balance is path dependent: rain dropped here is rain that never
    reaches the soil in any later step, so the zone over-irrigates forever after.
    The hour survives with t=None, which costs its ET instead (the safe direction).
    """
    base = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    stats = {
        # temperature only for 12:00 and 14:00 — 13:00 is a gap
        "sensor.t": [
            {"start": base, "mean": 25.0},
            {"start": base + timedelta(hours=2), "mean": 25.0},
        ],
        "sensor.r": [{"start": base + timedelta(hours=1), "change": 35.0}],
    }
    sensors = {
        "temperature": "sensor.t",
        "wind_speed": None,
        "solar_radiation": None,
        "dewpoint": None,
        "rain": "sensor.r",
    }
    hours = build_hours(stats, sensors, wind_height=2.0, longitude=10.0)

    assert len(hours) == 3  # 12:00, 13:00 (rain only), 14:00
    assert hours[1].t is None
    assert hours[1].precipitation_mm == 35.0
    assert sum(h.precipitation_mm for h in hours) == 35.0


def test_build_days_keeps_rain_only_days():
    stats = {
        "sensor.t": _hours("mean", 22.0, day=15),
        "sensor.r": _hours("change", 4.0, day=16),  # rain on a day with no temperature
    }
    days = build_days(stats, _all_sensors(rain="sensor.rain"), wind_height=2.0)
    assert len(days) == 1  # rain sensor not configured -> only the temperature day

    days = build_days(stats, _all_sensors(rain="sensor.r"), wind_height=2.0)
    assert len(days) == 2
    assert days[1].t_min is None and days[1].t_max is None
    assert abs(days[1].precipitation_mm - 12.0) < 1e-9  # 3 hours * 4.0
