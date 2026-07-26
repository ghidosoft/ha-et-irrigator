"""Home Assistant long-term statistics + history access layer.

Turns recorder statistics into the per-day aggregates consumed by ``calc.py`` and
finds the end of the last irrigation for a zone. All recorder I/O is dispatched
to the recorder's own executor (never the event loop).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calc import DayData, HourData
from .const import DEFAULT_WIND_SPEED

_LOGGER = logging.getLogger(__name__)

# Seconds of solar energy represented by one hourly statistic row.
_HOUR_SECONDS = 3600.0

# Normalise statistics to the units calc.py expects, regardless of how each
# sensor is configured. Keyed by HA unit *class*; only stats of that class are
# converted (e.g. a km/h wind sensor -> m/s, a °F temp sensor -> °C). Solar
# (W/m²) and rain (mm) have no entry and are read natively.
_TARGET_UNITS: dict[str, str] = {
    "temperature": UnitOfTemperature.CELSIUS,
    "speed": UnitOfSpeed.METERS_PER_SECOND,
}


def _row_start(row: dict[str, Any]) -> datetime:
    """Normalise a statistics row 'start' to an aware UTC datetime."""
    start = row["start"]
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return start


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


async def async_last_irrigation_end(
    hass: HomeAssistant,
    entity_id: str,
    window_start: datetime,
    now: datetime,
) -> datetime:
    """Return when the zone last finished watering, clamped to ``window_start``.

    The configured ``irrigation_sensor`` is expected to be a binary_sensor/switch
    that is ``on`` while the zone is watering. The reference point is the end of
    the most recent ``on`` period (or ``now`` if currently watering). If the
    sensor never turned on within the window, ``window_start`` is returned (the
    max-window safety cap).
    """
    changes = await get_instance(hass).async_add_executor_job(
        partial(
            history.state_changes_during_period,
            hass,
            window_start,
            now,
            entity_id,
            no_attributes=True,  # we only read `state`; skip the attribute rows
        )
    )
    states = changes.get(entity_id, [])

    last_on_end = window_start
    was_on = False
    for state in states:
        is_on = state.state == "on"
        if was_on and not is_on:
            last_on_end = state.last_changed
        was_on = is_on
    if was_on:  # still watering at 'now' -> deficit just reset
        last_on_end = now
    return max(last_on_end, window_start)


async def async_fetch_statistics(
    hass: HomeAssistant,
    statistic_ids: set[str],
    window_start: datetime,
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Pull hourly long-term statistics for the given ids over the window."""
    if not statistic_ids:
        return {}
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        window_start,
        now,
        statistic_ids,
        "hour",
        _TARGET_UNITS,
        {"mean", "min", "max", "change"},
    )


def _warn_if_rain_not_cumulative(
    stats: dict[str, list[dict[str, Any]]], rain_id: str | None
) -> None:
    """Rain needs a `total_increasing` sensor: HA only derives `change` from `sum`.

    If a rain sensor is configured and has rows but every `change` is None, it is
    almost certainly a `measurement` sensor -> precipitation would silently be 0.
    """
    if rain_id and rain_id in stats and stats[rain_id]:
        if all(r.get("change") is None for r in stats[rain_id]):
            _LOGGER.warning(
                "ET Irrigator: rain sensor '%s' returns no 'change' statistics. "
                "Set its state_class to 'total_increasing' (a cumulative mm total) "
                "or precipitation will be treated as zero",
                rain_id,
            )


def _solar_time_hours(
    midpoint_utc: datetime, longitude: float, day_of_year: int
) -> float:
    """Solar time at the hour midpoint [hours] — FAO-56 Eqs. 31-33.

    longitude is the site longitude in degrees (east positive, as in HA config).
    """
    utc_h = (
        midpoint_utc.hour
        + midpoint_utc.minute / 60.0
        + midpoint_utc.second / 3600.0
    )
    b = 2 * math.pi * (day_of_year - 81) / 364.0
    seasonal = 0.1645 * math.sin(2 * b) - 0.1255 * math.cos(b) - 0.025 * math.sin(b)
    return utc_h + longitude / 15.0 + seasonal


def build_hours(
    stats: dict[str, list[dict[str, Any]]],
    sensors: dict[str, str | None],
    wind_height: float,
    longitude: float,
) -> list[HourData]:
    """Build one :class:`HourData` per clock hour for the hourly ETo method.

    Channels are aligned by the statistic-row hour boundary. Solar radiation is
    integrated from W/m2 to MJ/m2/h; wind/dewpoint/temperature use the hourly
    mean; rain uses the per-hour ``change``. ``solar_time_hours`` is derived from
    the hour midpoint + site longitude for the FAO-56 hour angle.

    Hours are the **union** of the temperature and rain rows, not just the
    temperature ones: the step-by-step water balance is path dependent, so an hour
    whose temperature row is missing (recorder gap, sensor restart) must not take
    that hour's rain down with it. Such an hour gets ``t=None`` -> ETo 0.
    """

    def index(entity_id: str | None) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        if entity_id and entity_id in stats:
            for row in stats[entity_id]:
                out[int(_row_start(row).timestamp())] = row
        return out

    temp_ix = index(sensors.get("temperature"))
    wind_ix = index(sensors.get("wind_speed"))
    solar_ix = index(sensors.get("solar_radiation"))
    dew_ix = index(sensors.get("dewpoint"))
    rain_ix = index(sensors.get("rain"))

    _warn_if_rain_not_cumulative(stats, sensors.get("rain"))

    hours: list[HourData] = []
    for key in sorted(set(temp_ix) | set(rain_ix)):
        temp_row = temp_ix.get(key)
        t = temp_row.get("mean") if temp_row else None
        start = dt_util.utc_from_timestamp(key)
        midpoint = start + timedelta(minutes=30)
        doy = midpoint.timetuple().tm_yday  # UTC day-of-year (matches solar-time frame)

        wind_row = wind_ix.get(key)
        wind = wind_row.get("mean") if wind_row else None
        wind = wind if wind is not None else DEFAULT_WIND_SPEED

        solar_row = solar_ix.get(key)
        solar_mean = solar_row.get("mean") if solar_row else None
        solar_mj = (solar_mean or 0.0) * _HOUR_SECONDS / 1_000_000.0

        dew_row = dew_ix.get(key)
        dewpoint = dew_row.get("mean") if dew_row else None

        rain_row = rain_ix.get(key)
        precip = rain_row.get("change") if rain_row else None

        hours.append(
            HourData(
                day_of_year=doy,
                solar_time_hours=_solar_time_hours(midpoint, longitude, doy),
                t=t,
                solar_rad_mj=solar_mj,
                wind_speed=wind,
                wind_height=wind_height,
                dewpoint=dewpoint,
                precipitation_mm=precip if precip is not None else 0.0,
            )
        )
    return hours


def build_days(
    stats: dict[str, list[dict[str, Any]]],
    sensors: dict[str, str | None],
    wind_height: float,
) -> list[DayData]:
    """Aggregate hourly statistics into one :class:`DayData` per local day.

    ``sensors`` maps logical roles (temperature, dewpoint, wind_speed,
    solar_radiation, rain) to entity ids. Solar radiation rows are assumed in
    W/m2 and integrated to MJ/m2; other channels are averaged; rain uses the
    per-hour ``change``.

    Days are the **union** of the temperature and rain dates — see
    :func:`build_hours` for why a rain-only day must survive.
    """
    # Bucket each channel's rows by local date.
    def by_day(entity_id: str | None) -> dict[Any, list[dict[str, Any]]]:
        buckets: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        if entity_id and entity_id in stats:
            for row in stats[entity_id]:
                local = dt_util.as_local(_row_start(row))
                buckets[local.date()].append(row)
        return buckets

    temp_by_day = by_day(sensors.get("temperature"))
    wind_by_day = by_day(sensors.get("wind_speed"))
    solar_by_day = by_day(sensors.get("solar_radiation"))
    dew_by_day = by_day(sensors.get("dewpoint"))
    rain_by_day = by_day(sensors.get("rain"))

    _warn_if_rain_not_cumulative(stats, sensors.get("rain"))

    days: list[DayData] = []
    for day in sorted(set(temp_by_day) | set(rain_by_day)):
        temp_rows = temp_by_day.get(day, [])
        t_min = min((r["min"] for r in temp_rows if r.get("min") is not None), default=None)
        t_max = max((r["max"] for r in temp_rows if r.get("max") is not None), default=None)
        t_mean = _mean([r.get("mean") for r in temp_rows])

        # Distinguish genuine calm (0 m/s) from missing data: FAO-56 recommends a
        # 2 m/s default when wind data is unavailable, so don't collapse None -> 0.
        wind_mean = _mean([r.get("mean") for r in wind_by_day.get(day, [])])
        wind = wind_mean if wind_mean is not None else DEFAULT_WIND_SPEED

        solar_mj = sum(
            (r["mean"] * _HOUR_SECONDS / 1_000_000.0)
            for r in solar_by_day.get(day, [])
            if r.get("mean") is not None
        )

        dewpoint = _mean([r.get("mean") for r in dew_by_day.get(day, [])])

        precip = sum(
            r["change"] for r in rain_by_day.get(day, []) if r.get("change") is not None
        )

        days.append(
            DayData(
                day_of_year=day.timetuple().tm_yday,
                t_min=t_min,
                t_max=t_max,
                t_mean=t_mean,
                solar_rad_mj=solar_mj,
                wind_speed=wind,
                wind_height=wind_height,
                dewpoint=dewpoint,
                precipitation_mm=precip,
            )
        )
    return days
