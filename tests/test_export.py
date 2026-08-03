"""Round-trip tests for the per-hour statistics export.

These run against a real recorder (`recorder_mock`), so what is asserted is what
Home Assistant actually stores and hands back to a chart — not a mock's idea of
it. The property under test throughout is that the series read back as `change`
equals the mm that fell in each hour, and that `sum` never decreases, whatever
order of appends, rewrites and gaps produced it.
"""

from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.et_irrigator.statistics import async_export_hourly_series

STAT_ID = "sensor.et_irrigator_prato_hourly_rain"
BASE = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)


def _hours(values, *, start=BASE):
    """(hour start, value) pairs on consecutive hours from `start`."""
    return [(start + timedelta(hours=i), v) for i, v in enumerate(values)]


async def _read(hass, statistic_id=STAT_ID, *, start=BASE - timedelta(days=2)):
    """Read the stored series back as the chart would: change + sum, per hour."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        BASE + timedelta(days=3),
        {statistic_id},
        "hour",
        None,
        {"change", "sum"},
    )
    return [
        (dt_util.utc_from_timestamp(r["start"]), r["change"], r["sum"])
        for r in rows.get(statistic_id, [])
    ]


async def _export(hass, series, *, cutoff, rewrite=False, statistic_id=STAT_ID):
    through = await async_export_hourly_series(
        hass, statistic_id, "mm", series, cutoff=cutoff, rewrite=rewrite
    )
    await async_wait_recording_done(hass)
    return through


async def test_change_round_trips_as_the_mm_of_each_hour(recorder_mock, hass):
    """The whole point: hour H's bar is hour H's rain, not the cumulative."""
    values = [0.0, 0.6, 0.0, 1.2, 0.0]
    through = await _export(
        hass, _hours(values), cutoff=BASE + timedelta(hours=len(values))
    )

    stored = await _read(hass)
    assert [s for s, _, _ in stored] == [BASE + timedelta(hours=i) for i in range(5)]
    assert [round(ch, 4) for _, ch, _ in stored] == values
    assert through == BASE + timedelta(hours=4)

    sums = [s for _, _, s in stored]
    assert sums == sorted(sums), "sum must never decrease"


async def test_dry_hours_get_a_row_too(recorder_mock, hass):
    """A dense series is what lets the chart plot bars without bucketing."""
    await _export(hass, _hours([0.0, 0.0, 0.0]), cutoff=BASE + timedelta(hours=3))
    assert len(await _read(hass)) == 3


async def test_reexport_is_idempotent(recorder_mock, hass):
    """A restart re-runs the same export; it must update rows, never add them.

    This is the regression test for the phantom rainfall: the old level sensor
    grew a second, identical spike every time Home Assistant restarted.
    """
    values = [0.0, 0.6, 0.0, 1.2]
    cutoff = BASE + timedelta(hours=len(values))
    await _export(hass, _hours(values), cutoff=cutoff, rewrite=True)
    first = await _read(hass)

    await _export(hass, _hours(values), cutoff=cutoff, rewrite=True)
    second = await _read(hass)

    assert first == second
    assert len(second) == len(values)


async def test_the_partial_current_hour_is_never_written(recorder_mock, hass):
    """The in-progress hour is still accumulating rain, so it must wait."""
    values = [0.5, 0.5, 0.5]
    # cutoff at the third hour: only the two completed ones may be stored.
    await _export(hass, _hours(values), cutoff=BASE + timedelta(hours=2))
    stored = await _read(hass)
    assert [s for s, _, _ in stored] == [BASE, BASE + timedelta(hours=1)]


async def test_a_gap_is_filled_without_a_negative_change(recorder_mock, hass):
    """Home Assistant down for a few hours: the next cycle backfills, chained on."""
    await _export(hass, _hours([1.0, 2.0]), cutoff=BASE + timedelta(hours=2))

    # Six hours later, the same window plus what was missed.
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    await _export(hass, _hours(values), cutoff=BASE + timedelta(hours=6))

    stored = await _read(hass)
    assert [round(ch, 4) for _, ch, _ in stored] == values
    assert all(ch >= 0 for _, ch, _ in stored)
    sums = [s for _, _, s in stored]
    assert sums == sorted(sums)


async def test_append_leaves_already_stored_hours_alone(recorder_mock, hass):
    """Without rewrite, only hours after the newest stored one are emitted."""
    await _export(hass, _hours([1.0, 2.0]), cutoff=BASE + timedelta(hours=2))

    # Same hours, different values, plus a new one. The old rows must not move.
    await _export(hass, _hours([9.0, 9.0, 3.0]), cutoff=BASE + timedelta(hours=3))

    stored = await _read(hass)
    assert [round(ch, 4) for _, ch, _ in stored] == [1.0, 2.0, 3.0]


async def test_rewrite_redraws_the_window_and_keeps_sum_monotonic(recorder_mock, hass):
    """Retuning the cap changes past runoff; the junction must stay clean.

    The anchor is taken from the row *before* the rewritten window, so the rows
    that are not re-emitted still chain into the ones that are.
    """
    # An older stretch that stays put, then the window we will redraw.
    await _export(hass, _hours([1.0, 1.0], start=BASE), cutoff=BASE + timedelta(hours=2))
    later = BASE + timedelta(hours=2)
    await _export(hass, _hours([2.0, 2.0], start=later), cutoff=later + timedelta(hours=2))

    # Same hours, larger values (as if the infiltration cap had been raised).
    await _export(
        hass,
        _hours([5.0, 7.0], start=later),
        cutoff=later + timedelta(hours=2),
        rewrite=True,
    )

    stored = await _read(hass)
    assert [round(ch, 4) for _, ch, _ in stored] == [1.0, 1.0, 5.0, 7.0]
    sums = [s for _, _, s in stored]
    assert sums == sorted(sums), "the junction must not dip"


async def test_a_gap_longer_than_the_lookback_does_not_reset_the_cumulative(
    recorder_mock, hass
):
    """Away for longer than the anchor look-back: chain on, do not restart at 0.

    Restarting would push every later sum below the earlier ones and produce one
    huge negative bar at the junction — the failure this module exists to avoid.
    """
    await _export(hass, _hours([4.0, 4.0]), cutoff=BASE + timedelta(hours=2))
    before = await _read(hass)
    last_sum = before[-1][2]

    # Resume ten days later, far outside the one-day look-back window.
    much_later = BASE + timedelta(days=10)
    await _export(
        hass,
        _hours([1.0, 2.0], start=much_later),
        cutoff=much_later + timedelta(hours=2),
        rewrite=True,
    )

    stored = await _read(hass, start=BASE - timedelta(days=2))
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        BASE - timedelta(days=2),
        much_later + timedelta(days=1),
        {STAT_ID},
        "hour",
        None,
        {"change", "sum"},
    )
    sums = [r["sum"] for r in rows[STAT_ID]]
    assert sums == sorted(sums)
    assert sums[-1] == last_sum + 3.0
    assert stored, "the earlier rows are still there"


async def test_a_gauge_reset_cannot_produce_a_negative_bar(recorder_mock, hass):
    """A negative per-hour value (gauge rollover) is clamped, not propagated."""
    await _export(hass, _hours([1.0, -5.0, 2.0]), cutoff=BASE + timedelta(hours=3))
    stored = await _read(hass)
    assert [round(ch, 4) for _, ch, _ in stored] == [1.0, 0.0, 2.0]


async def test_an_empty_series_writes_nothing(recorder_mock, hass):
    assert await _export(hass, [], cutoff=BASE) is None
    assert await _read(hass) == []
