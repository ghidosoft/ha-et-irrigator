"""Component tests: YAML setup, sensor output, service, idempotency."""

from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.et_irrigator import coordinator as coord_mod
from custom_components.et_irrigator.calc import DayData
from custom_components.et_irrigator.const import DOMAIN, SERVICE_RECALCULATE

ENTITY = "sensor.et_irrigator_prato"

CONFIG = {
    DOMAIN: {
        "elevation": 250,
        "et_method": "daily",  # these wiring tests mock the daily data layer
        "sensors": {
            "temperature": "sensor.t",
            "dewpoint": "sensor.d",
            "wind_speed": "sensor.w",
            "solar_radiation": "sensor.s",
            "rain": "sensor.r",
        },
        "zones": [
            {
                "name": "Prato",
                "area": 50,
                "throughput": 12,
                "irrigation_sensor": "binary_sensor.iu",
            }
        ],
    }
}

_REF = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def expected_lingering_timers():
    """The coordinator keeps a long-lived fallback refresh timer (by design)."""
    return True


def _summer_day(precip=0.0) -> DayData:
    return DayData(
        day_of_year=196,
        t_min=18.0,
        t_max=30.0,
        solar_rad_mj=25.0,
        wind_speed=2.0,
        dewpoint=14.0,
        precipitation_mm=precip,
    )


@pytest.fixture
def patch_data(monkeypatch):
    """Replace the recorder-backed data layer with controllable canned data."""
    state = {"days": [_summer_day()]}

    async def fake_last_irrigation(hass, entity_id, window_start, now):
        return _REF

    async def fake_fetch(hass, ids, start, now):
        return {}

    def fake_build(stats, sensors, wind_height):
        return state["days"]

    monkeypatch.setattr(coord_mod, "async_last_irrigation_end", fake_last_irrigation)
    monkeypatch.setattr(coord_mod, "async_fetch_statistics", fake_fetch)
    monkeypatch.setattr(coord_mod, "build_days", fake_build)
    return state


async def test_setup_creates_zone_sensor_with_attributes(
    recorder_mock, hass, enable_et_irrigator, patch_data
):
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state is not None
    assert int(state.state) > 0  # dry summer day -> needs water

    attrs = state.attributes
    assert attrs["unit_of_measurement"] == "s"
    assert attrs["deficit"] > 0
    assert attrs["size"] == 50.0
    assert attrs["throughput"] == 12.0
    assert attrs["number_of_data_points"] == 1
    assert attrs["window_start"] == _REF.isoformat()
    assert "explanation" in attrs


async def test_heavy_rain_yields_zero_duration(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    async def fake_last_irrigation(hass, entity_id, window_start, now):
        return _REF

    async def fake_fetch(hass, ids, start, now):
        return {}

    def fake_build(stats, sensors, wind_height):
        return [_summer_day(precip=100.0)]

    monkeypatch.setattr(coord_mod, "async_last_irrigation_end", fake_last_irrigation)
    monkeypatch.setattr(coord_mod, "async_fetch_statistics", fake_fetch)
    monkeypatch.setattr(coord_mod, "build_days", fake_build)

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state.state == "0"
    assert state.attributes["deficit"] == 0.0


async def test_hourly_method_end_to_end(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """Default hourly method: build_hours -> compute_zone_hourly -> sensor."""
    from custom_components.et_irrigator.calc import HourData

    hourly_config = {DOMAIN: {**CONFIG[DOMAIN], "et_method": "hourly"}}

    async def fake_last(hass, entity_id, window_start, now):
        return _REF

    async def fake_fetch(hass, ids, start, now):
        return {}

    def fake_build_hours(stats, sensors, wind_height, longitude):
        # Six strong-sun midday hours -> a real (positive) deficit.
        return [
            HourData(
                day_of_year=196,
                solar_time_hours=10.5 + i,
                t=30.0,
                solar_rad_mj=2.6,
                wind_speed=2.0,
                dewpoint=14.0,
            )
            for i in range(6)
        ]

    monkeypatch.setattr(coord_mod, "async_last_irrigation_end", fake_last)
    monkeypatch.setattr(coord_mod, "async_fetch_statistics", fake_fetch)
    monkeypatch.setattr(coord_mod, "build_hours", fake_build_hours)

    assert await async_setup_component(hass, DOMAIN, hourly_config)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state is not None
    assert int(state.state) > 0
    assert state.attributes["number_of_data_points"] == 6
    assert "6h" in state.attributes["explanation"]


def _rain_hours(now, wet_index, wet_mm):
    """24 dry midday-ish hours ending before `now`, with rain in exactly one."""
    from custom_components.et_irrigator.calc import HourData

    first = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=24)
    return [
        HourData(
            day_of_year=196,
            solar_time_hours=10.5,
            t=30.0,
            solar_rad_mj=2.6,
            wind_speed=2.0,
            dewpoint=14.0,
            precipitation_mm=wet_mm if i == wet_index else 0.0,
            start=first + timedelta(hours=i),
        )
        for i in range(24)
    ]


async def _hourly_series(hass, statistic_id):
    """Read an exported series back the way the chart does."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=3),
        dt_util.utcnow() + timedelta(hours=1),
        {statistic_id},
        "hour",
        None,
        {"change", "sum"},
    )
    return [
        (dt_util.utc_from_timestamp(r["start"]), round(r["change"], 4), r["sum"])
        for r in rows.get(statistic_id, [])
    ]


def _patch_hourly(monkeypatch, hours):
    async def fake_last(hass, entity_id, window_start, now):
        return window_start

    async def fake_fetch(hass, ids, start, now):
        return {}

    monkeypatch.setattr(coord_mod, "async_last_irrigation_end", fake_last)
    monkeypatch.setattr(coord_mod, "async_fetch_statistics", fake_fetch)
    monkeypatch.setattr(
        coord_mod, "build_hours", lambda *a, **k: hours(dt_util.utcnow())
    )


async def test_exported_rain_lands_on_the_hour_it_fell_in(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """The regression test for the real bug.

    Rain falls in one hour; the coordinator only learns about it an hour later.
    The exported bar must sit on the hour it rained, not on the hour the
    coordinator happened to run — which is what a plain state would have given.
    """
    hourly_config = {DOMAIN: {**CONFIG[DOMAIN], "et_method": "hourly"}}
    _patch_hourly(monkeypatch, lambda now: _rain_hours(now, wet_index=5, wet_mm=0.6))

    assert await async_setup_component(hass, DOMAIN, hourly_config)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    series = await _hourly_series(hass, f"{ENTITY}_hourly_rain")
    wet = [(start, change) for start, change, _ in series if change > 0]
    assert len(wet) == 1, f"expected exactly one wet hour, got {wet}"

    expected = (
        dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=24)
        + timedelta(hours=5)
    )
    assert wet[0] == (expected, 0.6)
    # Every hour gets a row, dry ones included, so the chart needs no bucketing
    # of its own. All 24 here are complete: the fixture stops at the last full
    # hour, and the in-progress one is never offered.
    assert len(series) == 24
    assert all(change == 0.0 for start, change, _ in series if start != expected)


async def test_a_restart_adds_no_phantom_rainfall(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """Setting the integration up again must update rows, never add them.

    This is the exact failure that produced two 0.6 mm spikes from one shower:
    a restart rewrote the level sensor's state, and the chart drew it as a
    second rainfall. The export rewrites its whole window on setup, so this
    covers both the upsert and the correctness of the anchor lookup.
    """
    hourly_config = {DOMAIN: {**CONFIG[DOMAIN], "et_method": "hourly"}}
    _patch_hourly(monkeypatch, lambda now: _rain_hours(now, wet_index=5, wet_mm=0.6))

    assert await async_setup_component(hass, DOMAIN, hourly_config)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    before = await _hourly_series(hass, f"{ENTITY}_hourly_rain")

    # A restart: same inputs, another full-rewrite export.
    hass.data[DOMAIN].request_full_export()
    await hass.data[DOMAIN].async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    after = await _hourly_series(hass, f"{ENTITY}_hourly_rain")

    assert after == before
    assert sum(1 for _, change, _ in after if change > 0) == 1
    sums = [s for _, _, s in after]
    assert sums == sorted(sums)


async def test_runoff_and_drainage_split_the_lost_rain(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """The two ways rain is lost are published separately, on the right hours.

    30 mm in one hour against a 10 mm/h cap: 20 mm runs off *in that hour*, and
    what does infiltrate overflows a soil that is already near capacity. Keeping
    them apart is the point — runoff is the deterministic half, and the only one
    that says whether `max_infiltration_rate` is set correctly.
    """
    hourly_config = {
        DOMAIN: {
            **CONFIG[DOMAIN],
            "et_method": "hourly",
            "zones": [
                {
                    **CONFIG[DOMAIN]["zones"][0],
                    "max_infiltration_rate": 10.0,
                    "maximum_deficit": 5.0,
                }
            ],
        }
    }
    _patch_hourly(monkeypatch, lambda now: _rain_hours(now, wet_index=5, wet_mm=30.0))

    assert await async_setup_component(hass, DOMAIN, hourly_config)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    wet_hour = (
        dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=24)
        + timedelta(hours=5)
    )

    runoff = await _hourly_series(hass, f"{ENTITY}_hourly_runoff")
    assert [(s, c) for s, c, _ in runoff if c > 0] == [(wet_hour, 20.0)]

    drainage = await _hourly_series(hass, f"{ENTITY}_hourly_drainage")
    drained = [(s, c) for s, c, _ in drainage if c > 0]
    assert drained and drained[0][0] == wet_hour
    # Gross rain is untouched by the cap: it is what the gauge measured.
    rain = await _hourly_series(hass, f"{ENTITY}_hourly_rain")
    assert [(s, c) for s, c, _ in rain if c > 0] == [(wet_hour, 30.0)]


async def test_hourly_entities_are_stateless_anchors(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """They must exist for the chart, and carry no numeric state or state_class.

    A numeric state without a state_class trips the non-fixable
    `state_class_removed` repair; a state_class would make the recorder compile
    competing statistics for the same id.
    """
    hourly_config = {DOMAIN: {**CONFIG[DOMAIN], "et_method": "hourly"}}
    _patch_hourly(monkeypatch, lambda now: _rain_hours(now, wet_index=5, wet_mm=0.6))

    assert await async_setup_component(hass, DOMAIN, hourly_config)
    await hass.async_block_till_done()

    for suffix in ("hourly_rain", "hourly_runoff", "hourly_drainage"):
        state = hass.states.get(f"{ENTITY}_{suffix}")
        assert state is not None, f"missing {suffix} — the chart would not render"
        assert state.attributes.get("state_class") is None
        with pytest.raises(ValueError):
            float(state.state)
        assert state.attributes["unit_of_measurement"] == "mm"

    assert hass.states.get(ENTITY).attributes["hourly_export_through"] is not None


async def test_recalculate_service_is_idempotent_and_reflects_new_data(
    recorder_mock, hass, enable_et_irrigator, patch_data
):
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    first = hass.states.get(ENTITY).state

    # Same inputs -> identical result (idempotent).
    await hass.services.async_call(DOMAIN, SERVICE_RECALCULATE, {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == first

    # New data (more dry days) -> larger duration after recompute.
    patch_data["days"] = [_summer_day(), _summer_day(), _summer_day()]
    await hass.services.async_call(DOMAIN, SERVICE_RECALCULATE, {}, blocking=True)
    await hass.async_block_till_done()
    assert int(hass.states.get(ENTITY).state) > int(first)


async def test_reload_adds_and_removes_zones_without_restart(
    recorder_mock, hass, enable_et_irrigator, patch_data, monkeypatch
):
    """et_irrigator.reload applies YAML zone changes with no restart."""
    from custom_components.et_irrigator import CONFIG_SCHEMA

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.et_irrigator_prato") is not None
    assert hass.states.get("sensor.et_irrigator_giardino") is None

    def cfg(zones):
        return CONFIG_SCHEMA({DOMAIN: {**CONFIG[DOMAIN], "zones": zones}})

    prato = CONFIG[DOMAIN]["zones"][0]
    giardino = {
        "name": "Giardino",
        "area": 40,
        "throughput": 10,
        "irrigation_sensor": "binary_sensor.iu2",
    }
    holder = {"cfg": cfg([prato, giardino])}

    async def fake_yaml(hass, domain, **kwargs):
        return holder["cfg"]

    monkeypatch.setattr(
        "custom_components.et_irrigator.async_integration_yaml_config", fake_yaml
    )

    # Reload -> Giardino added, Prato kept.
    await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.et_irrigator_prato") is not None
    assert hass.states.get("sensor.et_irrigator_giardino") is not None

    # A zone owns several entities; the reload must add and drop all of them.
    assert hass.states.get("sensor.et_irrigator_giardino_deficit") is not None

    # Reload with only Giardino -> Prato removed.
    holder["cfg"] = cfg([giardino])
    await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.et_irrigator_prato") is None
    assert hass.states.get("sensor.et_irrigator_prato_deficit") is None
    assert hass.states.get("sensor.et_irrigator_prato_soil_moisture") is None
    assert hass.states.get("sensor.et_irrigator_giardino") is not None
    assert hass.states.get("sensor.et_irrigator_giardino_deficit") is not None


async def test_diagnostic_entities_are_graphable(
    recorder_mock, hass, enable_et_irrigator, patch_data
):
    """The water balance must be published as *states*, not just attributes.

    State attributes get no long-term statistics, so a deficit that lives only in
    an attribute cannot be graphed in the native History/Statistics cards. What
    makes these recordable is `state_class`, not `entity_category`.
    """
    from homeassistant.helpers import entity_registry as er

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for suffix, unit in (
        ("deficit", "mm"),
        ("net_deficit", "mm"),
        ("evapotranspiration", "mm"),
        ("precipitation", "mm"),
        ("rain_lost", "mm"),
        ("soil_moisture", "%"),
    ):
        entity_id = f"{ENTITY}_{suffix}"
        state = hass.states.get(entity_id)
        assert state is not None, f"missing {entity_id}"
        assert state.attributes["state_class"] == "measurement"
        assert state.attributes["unit_of_measurement"] == unit
        assert registry.async_get(entity_id).entity_category == er.EntityCategory.DIAGNOSTIC

    # The primary run-time sensor keeps its identity and is not diagnostic.
    assert registry.async_get(ENTITY).entity_category is None
    assert float(hass.states.get(f"{ENTITY}_deficit").state) > 0


async def test_diagnostic_entities_track_the_balance(
    recorder_mock, hass, enable_et_irrigator, monkeypatch
):
    """Surplus rain shows up as a negative net_deficit and as rain lost."""
    async def fake_last_irrigation(hass, entity_id, window_start, now):
        return _REF

    async def fake_fetch(hass, ids, start, now):
        return {}

    def fake_build(stats, sensors, wind_height):
        return [_summer_day(precip=100.0)]

    monkeypatch.setattr(coord_mod, "async_last_irrigation_end", fake_last_irrigation)
    monkeypatch.setattr(coord_mod, "async_fetch_statistics", fake_fetch)
    monkeypatch.setattr(coord_mod, "build_days", fake_build)

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    assert float(hass.states.get(f"{ENTITY}_deficit").state) == 0.0
    assert float(hass.states.get(f"{ENTITY}_net_deficit").state) < 0  # surplus
    assert float(hass.states.get(f"{ENTITY}_rain_lost").state) > 90.0
    assert float(hass.states.get(f"{ENTITY}_soil_moisture").state) == 100.0


def test_schema_accepts_precipitation_rate_zone():
    from custom_components.et_irrigator import CONFIG_SCHEMA

    cfg = CONFIG_SCHEMA(
        {
            DOMAIN: {
                **CONFIG[DOMAIN],
                "zones": [
                    {"name": "Rate Zone", "precipitation_rate": 12, "crop_coefficient": 0.8}
                ],
            }
        }
    )
    assert cfg[DOMAIN]["zones"][0]["precipitation_rate"] == 12.0


def test_schema_rejects_zone_without_rate_source():
    import voluptuous as vol
    from custom_components.et_irrigator import CONFIG_SCHEMA

    bad = {DOMAIN: {**CONFIG[DOMAIN], "zones": [{"name": "No Rate"}]}}
    try:
        CONFIG_SCHEMA(bad)
        assert False, "expected validation to fail"
    except vol.Invalid:
        pass


def test_schema_accepts_max_infiltration_rate():
    from custom_components.et_irrigator import CONFIG_SCHEMA

    cfg = CONFIG_SCHEMA(
        {
            DOMAIN: {
                **CONFIG[DOMAIN],
                "zones": [
                    {
                        "name": "Prato",
                        "precipitation_rate": 12,
                        "max_infiltration_rate": 15,
                    }
                ],
            }
        }
    )
    assert cfg[DOMAIN]["zones"][0]["max_infiltration_rate"] == 15.0


def test_schema_rejects_zero_maximum_deficit():
    """maximum_deficit is the bucket ceiling: 0 would silently zero the zone."""
    import voluptuous as vol
    from custom_components.et_irrigator import CONFIG_SCHEMA

    bad = {
        DOMAIN: {
            **CONFIG[DOMAIN],
            "zones": [
                {"name": "Prato", "precipitation_rate": 12, "maximum_deficit": 0}
            ],
        }
    }
    try:
        CONFIG_SCHEMA(bad)
        assert False, "expected validation to fail"
    except vol.Invalid:
        pass
