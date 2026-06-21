"""Pure unit tests for the evapotranspiration / water-balance / run-time math."""

import math

from custom_components.et_irrigator import pyeto
from custom_components.et_irrigator.calc import (
    DayData,
    ZoneCalcConfig,
    compute_zone,
    duration_seconds,
    eto_fao56_day,
)


def _summer_day(**overrides) -> DayData:
    base = dict(
        day_of_year=196,  # mid-July
        t_min=18.0,
        t_max=30.0,
        solar_rad_mj=25.0,  # MJ/m2/day, clear summer day
        wind_speed=2.0,
        wind_height=2.0,
        dewpoint=14.0,
    )
    base.update(overrides)
    return DayData(**base)


def _expected_eto(day: DayData, latitude: float, elevation: float) -> float:
    """Re-derive ETo straight from pyeto to anchor calc.py's wiring."""
    lat_rad = pyeto.deg2rad(latitude)
    svp = pyeto.mean_svp(day.t_min, day.t_max)
    avp = pyeto.avp_from_tdew(day.dewpoint)
    dsvp = pyeto.delta_svp(day.mean_temp)
    psy = pyeto.psy_const(pyeto.atm_pressure(elevation))
    ws2 = pyeto.wind_speed_2m(day.wind_speed, day.wind_height)
    sd = pyeto.sol_dec(day.day_of_year)
    sha = pyeto.sunset_hour_angle(lat_rad, sd)
    ird = pyeto.inv_rel_dist_earth_sun(day.day_of_year)
    extra = pyeto.et_rad(lat_rad, sd, sha, ird)
    csr = pyeto.cs_rad(elevation, extra)
    ni = pyeto.net_in_sol_rad(day.solar_rad_mj, albedo=0.23)
    no = pyeto.net_out_lw_rad(day.t_min, day.t_max, day.solar_rad_mj, csr, avp)
    nr = pyeto.net_rad(ni, no)
    return pyeto.fao56_penman_monteith(
        nr, pyeto.celsius2kelvin(day.mean_temp), ws2, svp, avp, dsvp, psy
    )


def test_eto_matches_raw_pyeto_pipeline():
    day = _summer_day()
    got = eto_fao56_day(day, latitude=45.0, elevation=250.0)
    assert got == _expected_eto(day, 45.0, 250.0)


def test_eto_summer_day_is_physically_sane():
    eto = eto_fao56_day(_summer_day(), latitude=45.0, elevation=250.0)
    # A clear hot mid-latitude summer day -> roughly 4-9 mm/day.
    assert 3.5 < eto < 9.0


def test_eto_increases_with_solar_radiation():
    low = eto_fao56_day(_summer_day(solar_rad_mj=10.0), 45.0, 250.0)
    high = eto_fao56_day(_summer_day(solar_rad_mj=28.0), 45.0, 250.0)
    assert high > low


def test_duration_formula_exact():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    # rate = 12 L/min * 60 / 50 m2 = 14.4 mm/h ; 5mm -> 5/14.4*3600 = 1250 s
    assert duration_seconds(5.0, cfg) == 1250


def test_duration_zero_when_no_deficit():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    assert duration_seconds(0.0, cfg) == 0
    assert duration_seconds(-3.0, cfg) == 0


def test_duration_respects_multiplier_cap_and_lead_time():
    cfg = ZoneCalcConfig(
        latitude=45,
        elevation=250,
        area=50.0,
        throughput=12.0,
        multiplier=2.0,
        maximum_duration=2000,
        lead_time=30,
    )
    # 5mm -> 1250s * 2 = 2500 -> capped 2000 -> +30 lead = 2030
    assert duration_seconds(5.0, cfg) == 2030


def test_compute_zone_rain_cancels_et():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    days = [_summer_day(precipitation_mm=100.0)]
    res = compute_zone(days, cfg)
    assert res.deficit == 0.0
    assert res.duration == 0
    assert res.delta > 0  # surplus


def test_compute_zone_accumulates_multi_day_deficit():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    one = compute_zone([_summer_day()], cfg)
    three = compute_zone([_summer_day(), _summer_day(), _summer_day()], cfg)
    assert three.evapotranspiration > one.evapotranspiration
    assert three.duration > one.duration


def test_compute_zone_deficit_capped_at_field_capacity():
    cfg = ZoneCalcConfig(
        latitude=45, elevation=250, area=50.0, throughput=12.0, maximum_deficit=5.0
    )
    days = [_summer_day() for _ in range(10)]  # huge ET
    res = compute_zone(days, cfg)
    assert res.deficit == 5.0


def test_compute_zone_is_idempotent():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    days = [_summer_day(), _summer_day(precipitation_mm=2.0)]
    a = compute_zone(days, cfg)
    b = compute_zone(days, cfg)
    assert a == b


def test_crop_coefficient_scales_et():
    base = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    high_kc = ZoneCalcConfig(
        latitude=45, elevation=250, area=50.0, throughput=12.0, crop_coefficient=1.5
    )
    days = [_summer_day()]
    assert math.isclose(
        compute_zone(days, high_kc).evapotranspiration,
        compute_zone(days, base).evapotranspiration * 1.5,
        rel_tol=1e-3,  # 3-decimal rounding noise on the stored value
    )
