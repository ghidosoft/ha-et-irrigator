"""Component tests: YAML setup, sensor output, service, idempotency."""

from datetime import datetime, timezone

import pytest

from homeassistant.setup import async_setup_component

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

    # Reload with only Giardino -> Prato removed.
    holder["cfg"] = cfg([giardino])
    await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.et_irrigator_prato") is None
    assert hass.states.get("sensor.et_irrigator_giardino") is not None
