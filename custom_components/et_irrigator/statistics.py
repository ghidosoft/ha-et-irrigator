"""Home Assistant long-term statistics + history access layer.

Turns recorder statistics into the per-day aggregates consumed by ``calc.py``,
finds the end of the last irrigation for a zone, and writes our own per-hour
series back into the recorder's long-term statistics. Every *reading* path is
dispatched to the recorder's own executor (never the event loop); the write path
is ``async_import_statistics``, which is a callback that only queues a job.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calc import DayData, HourData
from .const import DEFAULT_WIND_SPEED, SOLAR_STUCK_MIN_HOURS

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


def slice_stats(
    stats: dict[str, list[dict[str, Any]]], since: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Drop the rows older than ``since`` from every series.

    The fetch spans the full export window while the water balance only runs from
    the end of the last irrigation, so the balance-facing builders get a sliced
    copy and behave exactly as they did when the fetch itself was narrower.
    """
    return {
        entity_id: [row for row in rows if _row_start(row) >= since]
        for entity_id, rows in stats.items()
    }


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


def _warn_if_solar_stuck(
    solar_id: str | None, stuck: set[int], index: dict[int, dict[str, Any]]
) -> None:
    """Report a stuck pyranometer: it is silent everywhere else.

    Only runs pinned above the window's darkest value are reported — a run at the
    dark offset is just night, which every window contains.
    """
    if not solar_id or not stuck:
        return
    floor = min(
        (r["mean"] for r in index.values() if r.get("mean") is not None), default=None
    )
    lit = [k for k in stuck if floor is not None and (index[k].get("mean") or 0.0) > floor]
    if not lit:
        return
    _LOGGER.warning(
        "ET Irrigator: solar sensor '%s' reports %d hour(s) frozen at a single value "
        "(from %s). Those hours fall back to the FAO-56 temperature-range estimate; "
        "check the sensor",
        solar_id,
        len(lit),
        dt_util.utc_from_timestamp(min(lit)).isoformat(),
    )


def _stuck_hours(index: dict[int, dict[str, Any]]) -> set[int]:
    """Hour keys whose row is a carried-forward state rather than a measurement.

    A statistics row with ``min == max`` never moved for the whole hour. Real
    irradiance never does that under daylight, so a run of consecutive hours pinned
    to one identical value marks the span where the sensor stopped reporting and HA
    kept republishing its last state — the entity stays available and the recorder
    keeps compiling rows, which is exactly why this is invisible from the state.

    Night is pinned too, at the sensor's dark offset, and is reported as stuck like
    any other run. That is harmless: the substitute for a dark hour is 0 anyway
    because the extraterrestrial radiation is 0. Callers that need to tell the two
    apart (the daily method) compare against the day's own floor.

    One isolated hour is not enough — an overcast hour can legitimately sit still —
    hence ``SOLAR_STUCK_MIN_HOURS``.
    """
    stuck: set[int] = set()
    run: list[int] = []

    def flush() -> None:
        if len(run) >= SOLAR_STUCK_MIN_HOURS:
            stuck.update(run)

    previous: int | None = None
    for key in sorted(index):
        row = index[key]
        low, high = row.get("min"), row.get("max")
        pinned = low is not None and high is not None and low == high
        # A recorder gap breaks the run: two pinned hours either side of a hole are
        # not evidence that the sensor sat still through it.
        contiguous = previous is not None and key - previous == _HOUR_SECONDS
        if pinned and contiguous and run and row.get("mean") == index[run[-1]].get("mean"):
            run.append(key)
        else:
            flush()
            run = [key] if pinned else []
        previous = key
    flush()
    return stuck


def _daily_temp_range(
    temp_index: dict[int, dict[str, Any]]
) -> dict[Any, tuple[float | None, float | None]]:
    """Local date -> (min, max) temperature, to feed the Rs fallback per hour."""
    low: dict[Any, float] = {}
    high: dict[Any, float] = {}
    for key, row in temp_index.items():
        day = dt_util.as_local(dt_util.utc_from_timestamp(key)).date()
        if (value := row.get("min")) is not None:
            low[day] = min(low[day], value) if day in low else value
        if (value := row.get("max")) is not None:
            high[day] = max(high[day], value) if day in high else value
    return {day: (low.get(day), high.get(day)) for day in set(low) | set(high)}


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

    solar_stuck = _stuck_hours(solar_ix)
    temp_range = _daily_temp_range(temp_ix)
    _warn_if_solar_stuck(sensors.get("solar_radiation"), solar_stuck, solar_ix)

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
        # None, not 0.0: a missing or stuck row is "no reading", which eto_fao56_hourly
        # replaces with the FAO-56 temperature-range estimate. Zeroing it here would
        # under-state ET as silently as the stuck value over-states it.
        solar_mj = (
            None
            if solar_mean is None or key in solar_stuck
            else solar_mean * _HOUR_SECONDS / 1_000_000.0
        )
        t_min_day, t_max_day = temp_range.get(dt_util.as_local(start).date(), (None, None))

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
                start=start,
                t_min_day=t_min_day,
                t_max_day=t_max_day,
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

    solar_ix = {
        int(_row_start(row).timestamp()): row
        for row in stats.get(sensors.get("solar_radiation") or "", [])
    }
    solar_stuck = _stuck_hours(solar_ix)
    _warn_if_solar_stuck(sensors.get("solar_radiation"), solar_stuck, solar_ix)

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

        solar_rows = solar_by_day.get(day, [])
        # A daily total is only a measurement if all of its hours were. Unlike the
        # hourly method, which can replace single hours, the day is all-or-nothing:
        # summing the surviving hours would pass off a partial total as a full one.
        # The dark offset is excluded by comparing against the day's own floor —
        # every day has a pinned night, and that is not a fault.
        solar_floor = min(
            (r["mean"] for r in solar_rows if r.get("mean") is not None), default=None
        )
        day_stuck = solar_floor is not None and any(
            int(_row_start(r).timestamp()) in solar_stuck
            and r.get("mean") is not None
            and r["mean"] > solar_floor
            for r in solar_rows
        )
        # ``solar_floor is None`` means the day has no usable row at all — no solar
        # sensor configured, or an all-day recorder gap. That is "no reading", the
        # same as a stuck day; summing to 0.0 would report a pitch-black day and
        # under-state ET, where ``build_hours`` already estimates those hours.
        solar_mj = (
            None
            if day_stuck or solar_floor is None
            else sum(
                (r["mean"] * _HOUR_SECONDS / 1_000_000.0)
                for r in solar_rows
                if r.get("mean") is not None
            )
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


# ---------------------------------------------------------------------------
# Writing our own per-hour series into the long-term statistics.
#
# Why import statistics at all instead of publishing a state: the coordinator
# only learns how many mm fell during hour H once the recorder has compiled H's
# statistics, at ~H+1:12. A *state* written then is timestamped then, so the bar
# lands an hour late — and a Home Assistant restart writes a fresh state row with
# an unchanged value, which a column chart draws as a second, phantom rainfall.
# `async_import_statistics` is the only mechanism that can write a value stamped
# at hour H after hour H is over, and it upserts on (statistic_id, start), so a
# restart updates rows instead of adding them.
#
# The entities behind these ids deliberately have `state_class = None` and no
# numeric state; see sensor.py for why both halves of that are required.
# ---------------------------------------------------------------------------


def _statistic_metadata(statistic_id: str, unit: str) -> StatisticMetaData:
    """Metadata for one of our imported per-hour series.

    ``source`` must be the recorder's own domain: that is what
    `async_import_statistics` accepts for a real ``sensor.*`` entity id.

    ``mean_type`` and ``unit_class`` are both required. Home Assistant raises
    outright on a missing ``mean_type``, and while it tolerates a missing
    ``unit_class`` on the first insert, every later import of the same id goes
    through the metadata *update* path, which reads the key directly and would
    fail with a KeyError — so the series would be written once and then never
    again. ``has_mean`` is deliberately absent: it is the deprecated spelling of
    ``mean_type`` and carries no extra information.

    ``unit_class`` is looked up rather than hardcoded so it matches whatever the
    recorder itself would pick for this unit (mm -> "distance"), which is what
    lets the statistics API convert the series for display.
    """
    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,  # a sum-only series has no mean
        name=None,
        source=RECORDER_DOMAIN,
        statistic_id=statistic_id,
        unit_class=converter.UNIT_CLASS if converter else None,
        unit_of_measurement=unit,
    )


async def _async_stored_sums(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Existing (hour, cumulative sum) rows for a statistic, oldest first."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [
        (_row_start(row), row["sum"])
        for row in rows.get(statistic_id, [])
        if row.get("sum") is not None
    ]


async def _async_last_sum_before(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float | None:
    """Cumulative sum of the newest stored row older than ``before``, if any.

    Only used when the bounded look-back found nothing, to tell a genuine first
    run apart from a gap longer than that look-back (Home Assistant down for
    days, or ``max_window_days`` widened in YAML). Restarting the cumulative at 0
    in the latter case would push every later sum down and produce one large
    negative ``change`` at the junction — exactly the kind of phantom bar this
    module exists to remove.

    `get_last_statistics` is not usable as the normal anchor: it returns the most
    recent row overall, which sits *inside* the window we are about to rewrite.
    """
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, False, {"sum"}
    )
    for row in rows.get(statistic_id, []):
        if row.get("sum") is not None and _row_start(row) < before:
            return row["sum"]
    return None


async def async_export_hourly_series(
    hass: HomeAssistant,
    statistic_id: str,
    unit: str,
    series: list[tuple[datetime, float]],
    *,
    cutoff: datetime,
    rewrite: bool,
) -> datetime | None:
    """Publish ``series`` as hourly long-term statistics; return the newest hour stored.

    ``series`` is ``(UTC hour start, value during that hour)``. Values are turned
    into a running cumulative because Home Assistant derives a per-hour figure as
    ``change = sum[h] - sum[h-1]``; the chart reads ``change``, never the state.

    One row is emitted for **every** hour of the series, zeros included. A dense
    series is what lets the chart plot bars straight from ``change`` without any
    bucketing of its own.

    ``cutoff`` is the top of the current hour, exclusive: the in-progress hour is
    never written, because its rain is still accumulating.

    ``rewrite`` re-emits the whole series from an anchor taken *before* it;
    otherwise only the hours after the newest stored one are appended (which also
    covers first runs and fills gaps, since "everything after nothing" is
    everything).

    Rewriting is safe because every exported quantity is ``>= 0``: a rewrite can
    only raise a cumulative, never lower it, so ``sum`` stays non-decreasing and
    ``change`` can never go negative. Callers must only pass path-dependent
    quantities (drainage) with ``rewrite=False``, since for those the same clock
    hour can legitimately yield a different value from a shifted window.
    """
    hours = sorted((start, value) for start, value in series if start < cutoff)
    if not hours:
        return None

    series_start = hours[0][0]
    stored = await _async_stored_sums(
        hass, statistic_id, series_start - timedelta(days=1), cutoff
    )

    newest_stored = stored[-1][0] if stored else None
    if rewrite:
        earlier = [row for row in stored if row[0] < series_start]
        anchor = earlier[-1][1] if earlier else None
        pending = hours
    else:
        anchor = stored[-1][1] if stored else None
        pending = (
            hours
            if newest_stored is None
            else [row for row in hours if row[0] > newest_stored]
        )

    if anchor is None:
        anchor = await _async_last_sum_before(hass, statistic_id, series_start)
    total = anchor or 0.0

    if not pending:
        return newest_stored

    # A fresh list every call: _async_import_statistics rewrites each dict's
    # "start" in place and hands the very same object to the recorder queue.
    stats: list[StatisticData] = []
    for start, value in pending:
        # Clamped at 0 so a rain gauge that resets (negative `change`) cannot
        # pull the cumulative down and manufacture a negative bar.
        total += max(value, 0.0)
        # `state` is written alongside `sum` on purpose: the recorder's update
        # path uses statistic.get(...), so any field left out of a re-import is
        # stored as NULL rather than left alone.
        stats.append(
            StatisticData(start=start, state=round(total, 4), sum=round(total, 4))
        )

    async_import_statistics(hass, _statistic_metadata(statistic_id, unit), stats)

    newest_written = pending[-1][0]
    if newest_stored is None:
        return newest_written
    return max(newest_written, newest_stored)


async def async_clear_hourly_series(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Drop imported series for zones that no longer exist.

    Without this the statistics outlive the entity and `validate_statistics`
    reports them as `no_state` forever.
    """
    if statistic_ids:
        get_instance(hass).async_clear_statistics(statistic_ids)
