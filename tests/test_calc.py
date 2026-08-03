"""Pure unit tests for the evapotranspiration / water-balance / run-time math."""

import math

from custom_components.et_irrigator import pyeto
from custom_components.et_irrigator.calc import (
    DayData,
    HourData,
    ZoneCalcConfig,
    compute_zone,
    compute_zone_hourly,
    duration_seconds,
    eto_fao56_day,
    eto_fao56_hourly,
    ra_hourly,
    run_water_balance,
    step_cap,
    step_infiltration,
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
    no = pyeto.net_out_lw_rad(
        pyeto.celsius2kelvin(day.t_min),
        pyeto.celsius2kelvin(day.t_max),
        day.solar_rad_mj,
        csr,
        avp,
    )
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
    # Clear hot mid-latitude summer day. Tight band: the previous loose 3.5-9.0
    # let a Celsius/Kelvin bug in the longwave term (which inflated ETo ~16%)
    # slip through. With longwave applied correctly this sits ~5.5-6.5 mm/day.
    assert 5.0 < eto < 7.0


def test_longwave_loss_is_applied():
    """Guard the Kelvin bug directly: outgoing longwave must reduce ETo.

    If net_out_lw_rad is fed Celsius the longwave term collapses to ~0 and ETo
    jumps. This asserts the longwave loss is materially non-zero.
    """
    from custom_components.et_irrigator import pyeto

    day = _summer_day()
    lat_rad = pyeto.deg2rad(45.0)
    sd = pyeto.sol_dec(day.day_of_year)
    sha = pyeto.sunset_hour_angle(lat_rad, sd)
    ird = pyeto.inv_rel_dist_earth_sun(day.day_of_year)
    csr = pyeto.cs_rad(250.0, pyeto.et_rad(lat_rad, sd, sha, ird))
    no_lw = pyeto.net_out_lw_rad(
        pyeto.celsius2kelvin(day.t_min),
        pyeto.celsius2kelvin(day.t_max),
        day.solar_rad_mj,
        csr,
        pyeto.avp_from_tdew(day.dewpoint),
    )
    assert no_lw > 3.0  # realistic clear-day net longwave is several MJ/m²/day, not ~0


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
    assert res.net_deficit < 0  # same number, deficit sign convention
    assert res.drainage > 0  # the surplus left the root zone, it was not banked


def test_rain_beyond_capacity_is_drained_not_banked():
    """The reported bug, at its root: surplus rain must not pay for later ET.

    Old model: sum(rain) - sum(ET*Kc) over the window, so 100 mm of rain silently
    cancelled every subsequent dry day until it scrolled out of the window. The
    bucket drains it the day it falls, so the very next dry day shows a deficit.
    """
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    wet_then_dry = compute_zone([_summer_day(precipitation_mm=100.0), _summer_day()], cfg)
    dry_only = compute_zone([_summer_day()], cfg)

    assert wet_then_dry.deficit > 0
    assert wet_then_dry.duration > 0
    # The dry day's ET is charged in full: the 100 mm bought nothing beyond its day.
    assert math.isclose(wet_then_dry.deficit, dry_only.deficit, rel_tol=1e-6)
    assert wet_then_dry.drainage > 90.0


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
    # The ET the ceiling refused to account for: a chronic under-watering signal.
    assert res.capped > 0


# --- The water balance itself -----------------------------------------------

def test_water_balance_unclamped_matches_plain_sum():
    """With an unreachable ceiling, depletion == net_deficit + drainage exactly."""
    wb = run_water_balance(
        [2.0, 3.0, 1.0, 4.0], [0.0, 10.0, 0.0, 0.0], taw=1e9
    )
    assert math.isclose(wb.depletion, wb.net_deficit + wb.drainage)
    assert math.isclose(wb.net_deficit, sum([2.0, 3.0, 1.0, 4.0]) - 10.0)


def test_water_balance_stays_within_bounds():
    etc = [0.0, 5.0, 0.3, 9.0, 0.0, 2.5, 7.0, 0.1]
    rain = [12.0, 0.0, 0.0, 3.0, 40.0, 0.0, 0.0, 1.0]
    for taw in (1.0, 6.0, 30.0):
        wb = run_water_balance(etc, rain, taw=taw)
        assert 0.0 <= wb.depletion <= taw


def test_water_balance_conserves_water():
    """Every mm of rain is either infiltrated or run off; ET is fully accounted."""
    etc = [1.0, 2.0, 0.5]
    rain = [30.0, 0.0, 4.0]
    wb = run_water_balance(etc, rain, taw=10.0, max_infiltration_rate=8.0)
    assert math.isclose(wb.precipitation, sum(rain))
    assert math.isclose(wb.infiltration + wb.runoff, wb.precipitation)
    assert math.isclose(wb.evapotranspiration, sum(etc))
    # depletion = ETc - infiltration + drained + capped
    assert math.isclose(
        wb.depletion,
        wb.evapotranspiration - wb.infiltration + wb.drainage + wb.capped,
    )


def test_infiltration_rate_cap_creates_runoff():
    wb = run_water_balance([0.0], [30.0], taw=100.0, max_infiltration_rate=10.0)
    assert wb.precipitation == 30.0
    assert wb.infiltration == 10.0
    assert wb.runoff == 20.0


def test_infiltration_cap_is_a_noop_on_light_rain():
    """Unlike a flat efficiency factor, the cap must not touch a gentle drizzle."""
    wb = run_water_balance([0.0], [2.0], taw=100.0, max_infiltration_rate=10.0)
    assert wb.infiltration == 2.0
    assert wb.runoff == 0.0


def test_infiltration_cap_scales_with_step_length():
    """A daily step gets 24h worth of infiltration allowance, not 1h."""
    hourly = run_water_balance([0.0], [30.0], taw=100.0, max_infiltration_rate=10.0)
    daily = run_water_balance(
        [0.0], [30.0], taw=100.0, step_hours=24.0, max_infiltration_rate=10.0
    )
    assert hourly.runoff == 20.0
    assert daily.runoff == 0.0


def test_steps_sum_back_to_the_totals():
    """The per-step trace must be a decomposition of the window, not a parallel guess."""
    etc = [1.0, 2.0, 0.5, 3.0]
    rain = [30.0, 0.0, 4.0, 1.0]
    wb = run_water_balance(etc, rain, taw=10.0, max_infiltration_rate=8.0)

    assert len(wb.steps) == len(etc)
    assert math.isclose(sum(s.precipitation for s in wb.steps), wb.precipitation)
    assert math.isclose(sum(s.infiltration for s in wb.steps), wb.infiltration)
    assert math.isclose(sum(s.runoff for s in wb.steps), wb.runoff)
    assert math.isclose(sum(s.evapotranspiration for s in wb.steps), wb.evapotranspiration)
    # drainage is the accumulator; each step records only its own delta
    assert math.isclose(sum(s.drainage for s in wb.steps), wb.drainage)
    assert math.isclose(wb.steps[-1].depletion, wb.depletion)


def test_step_rain_and_runoff_are_window_independent():
    """The contract the hourly export rests on.

    Per step, precipitation and runoff are pure functions of that step's mm and
    the cap, so they are identical whether or not the window started earlier.
    Drainage is not: it follows the bucket's trajectory. Rewriting the first two
    in place is therefore safe; rewriting drainage is not.
    """
    # A dry prefix, so the longer window reaches the tail with a part-empty
    # bucket while the short one starts at field capacity. Without that the
    # clamps erase the difference and the two agree by accident.
    etc = [4.0, 3.0, 0.0, 0.0, 0.0, 0.0]
    rain = [0.0, 0.0, 0.0, 5.0, 30.0, 0.0]

    full = run_water_balance(etc, rain, taw=10.0, max_infiltration_rate=8.0)
    late = run_water_balance(etc[3:], rain[3:], taw=10.0, max_infiltration_rate=8.0)

    tail = full.steps[3:]
    assert [s.precipitation for s in tail] == [s.precipitation for s in late.steps]
    assert [s.runoff for s in tail] == [s.runoff for s in late.steps]
    assert [s.infiltration for s in tail] == [s.infiltration for s in late.steps]

    # The counterexample: the same clock hours drain differently, because the
    # earlier window arrives at them with a different bucket level.
    assert [s.drainage for s in tail] != [s.drainage for s in late.steps]


def test_step_infiltration_matches_the_loop():
    """The export and the balance must share one expression, not two equivalent ones."""
    assert step_cap(None, 1.0) is None
    assert step_cap(0.0, 1.0) is None  # falsy disables the cap, as in the loop
    assert step_cap(10.0, 24.0) == 240.0

    assert step_infiltration(30.0, None) == 30.0  # uncapped: everything soaks in
    assert step_infiltration(30.0, 10.0) == 10.0
    assert step_infiltration(2.0, 10.0) == 2.0  # a drizzle is untouched

    cap = step_cap(8.0, 1.0)
    wb = run_water_balance([0.0], [30.0], taw=100.0, max_infiltration_rate=8.0)
    assert wb.steps[0].runoff == 30.0 - step_infiltration(30.0, cap)


def test_net_deficit_sign_convention():
    """net_deficit is a deficit (positive = dry); delta is its legacy inverse."""
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    wet = compute_zone([_summer_day(precipitation_mm=100.0)], cfg)
    dry = compute_zone([_summer_day()], cfg)
    assert wet.net_deficit < 0 < dry.net_deficit
    assert wet.delta == -wet.net_deficit
    assert dry.delta == -dry.net_deficit


def test_eto_is_zero_when_temperature_is_missing():
    """A recorder gap must cost ET, never the hour's rain."""
    assert eto_fao56_day(_summer_day(t_min=None, t_max=None), 45.0, 250.0) == 0.0
    assert eto_fao56_hourly(_solar_hour(12.5, t=None), 45.0, 250.0) == 0.0


def test_rain_survives_a_missing_temperature_step():
    cfg = ZoneCalcConfig(latitude=45, elevation=250, area=50.0, throughput=12.0)
    gap = DayData(
        day_of_year=196, t_min=None, t_max=None, solar_rad_mj=0.0,
        wind_speed=2.0, precipitation_mm=8.0,
    )
    res = compute_zone([_summer_day(), gap], cfg)
    assert res.precipitation == 8.0
    assert res.infiltration == 8.0
    assert res.deficit == 0.0  # 8 mm covers the one summer day's ET


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


# --- Hourly FAO-56 method ---------------------------------------------------

def _solar_hour(hour_of_day, **overrides):
    """A synthetic HourData where clock == solar time (longitude ignored)."""
    base = dict(
        day_of_year=196,
        solar_time_hours=hour_of_day,
        t=24.0,
        solar_rad_mj=2.0,
        wind_speed=2.0,
        dewpoint=16.0,
    )
    base.update(overrides)
    return HourData(**base)


def test_ra_hourly_zero_at_night_positive_at_noon():
    noon = ra_hourly(45.0, 196, 12.5)
    night = ra_hourly(45.0, 196, 2.5)
    assert noon > 0.0
    assert night == 0.0


def test_eto_hourly_noon_is_sane_and_positive():
    # Hot sunny midday hour -> a fraction of a mm in one hour.
    eto = eto_fao56_hourly(_solar_hour(13.0, t=32.0, solar_rad_mj=3.0), 45.0, 160.0)
    assert 0.1 < eto < 1.2


def test_eto_hourly_night_lower_than_day():
    day = eto_fao56_hourly(_solar_hour(13.0, t=30.0, solar_rad_mj=3.0), 45.0, 160.0)
    night = eto_fao56_hourly(_solar_hour(2.0, t=20.0, solar_rad_mj=0.0), 45.0, 160.0)
    assert night < day


def _synthetic_day_hours(doy=196):
    """24 hours of a clear sinusoidal summer day (clock == solar time)."""
    hours = []
    for h in range(24):
        x = math.cos((h + 0.5 - 12) / 12 * math.pi)
        solar_w = max(0.0, 900 * x) if 5 < (h + 0.5) < 19 else 0.0
        t = 29 - 7 * math.cos((h + 0.5 - 15) / 12 * math.pi)
        hours.append(
            HourData(
                day_of_year=doy,
                solar_time_hours=h + 0.5,
                t=t,
                solar_rad_mj=solar_w * 3600 / 1e6,
                wind_speed=1.5,
                dewpoint=19.0,
            )
        )
    return hours


def test_hourly_sum_close_to_daily():
    """Summed 24 hourly ETo should track the daily PM value within ~15%."""
    hours = _synthetic_day_hours()
    eto_hourly_sum = sum(eto_fao56_hourly(h, 45.0, 160.0) for h in hours)

    solar_day = sum(h.solar_rad_mj for h in hours)
    tmin = min(h.t for h in hours)
    tmax = max(h.t for h in hours)
    day = DayData(
        day_of_year=196, t_min=tmin, t_max=tmax, solar_rad_mj=solar_day,
        wind_speed=1.5, dewpoint=19.0,
    )
    eto_daily = eto_fao56_day(day, 45.0, 160.0)
    assert abs(eto_hourly_sum - eto_daily) / eto_daily < 0.15


def test_hourly_deficit_is_monotonic():
    """Adding completed hours can only grow (never shrink) the deficit."""
    cfg = ZoneCalcConfig(latitude=45, elevation=160, area=50.0, throughput=12.0)
    hours = _synthetic_day_hours()
    prev = -1.0
    for n in range(1, len(hours) + 1):
        d = compute_zone_hourly(hours[:n], cfg).deficit
        assert d >= prev - 1e-9  # non-decreasing
        prev = d


def test_compute_zone_hourly_publishes_one_step_per_hour():
    """The export aligns steps to hours by position, so the two must not drift."""
    cfg = ZoneCalcConfig(latitude=45, elevation=160, area=50.0, throughput=12.0)
    hours = _synthetic_day_hours()
    res = compute_zone_hourly(hours, cfg)
    assert len(res.steps) == len(hours)
    assert math.isclose(sum(s.precipitation for s in res.steps), res.precipitation, abs_tol=1e-3)


def test_compute_zone_hourly_rain_refills_then_et_resumes():
    """50 mm at noon fills the soil; the afternoon's ET starts drying it again.

    Under the old sum-then-clamp model this returned deficit 0 for the whole day
    (and for every day after, until the rain hour scrolled out of the window).
    """
    cfg = ZoneCalcConfig(latitude=45, elevation=160, area=50.0, throughput=12.0)
    hours = _synthetic_day_hours()
    hours[12] = HourData(**{**hours[12].__dict__, "precipitation_mm": 50.0})
    res = compute_zone_hourly(hours, cfg)

    afternoon_et = sum(eto_fao56_hourly(h, 45.0, 160.0) for h in hours[13:])
    assert math.isclose(res.deficit, afternoon_et, abs_tol=1e-3)  # deficit is rounded
    assert res.duration > 0
    assert res.drainage > 40.0


def test_no_cliff_when_rain_leaves_the_window():
    """Regression test for the reported bug: no step change as the window slides.

    Old model: while a big rain event sat inside the window it cancelled the whole
    window's ET (deficit 0); the hour it scrolled out, the deficit jumped straight
    to `maximum_deficit`. With per-step clamping, dropping the oldest hour can only
    move the answer by that hour's net drying, because every step is monotone and
    1-Lipschitz and composition preserves both.
    """
    cfg = ZoneCalcConfig(
        latitude=45, elevation=160, area=50.0, throughput=12.0, maximum_deficit=30.0
    )
    hours: list[HourData] = []
    for _ in range(7):  # a 7-day window, like the default max_window_days
        hours.extend(_synthetic_day_hours())
    hours[2 * 24 + 15] = HourData(
        **{**hours[2 * 24 + 15].__dict__, "precipitation_mm": 35.0}
    )

    max_hourly_etc = max(eto_fao56_hourly(h, 45.0, 160.0) for h in hours)
    deficits = [compute_zone_hourly(hours[k:], cfg).deficit for k in range(len(hours))]
    steps = [abs(b - a) for a, b in zip(deficits, deficits[1:])]

    # +1e-3 covers the 3-decimal rounding of the two published deficits.
    assert max(steps) <= max_hourly_etc + 1e-3
    # Sanity: the scenario really does exercise a full dry-down, so a cliff would
    # have had somewhere to fall from.
    assert max(deficits) > 20.0


# --- Application rate: precipitation_rate vs area+throughput ----------------

def test_rate_from_area_throughput():
    cfg = ZoneCalcConfig(latitude=45, elevation=160, area=30.0, throughput=10.0)
    assert cfg.rate_mm_h == 10.0 * 60 / 30.0  # 20 mm/h


def test_rate_from_precipitation_rate_direct():
    cfg = ZoneCalcConfig(latitude=45, elevation=160, precipitation_rate=12.0)
    assert cfg.rate_mm_h == 12.0


def test_precipitation_rate_takes_priority_over_area_throughput():
    cfg = ZoneCalcConfig(
        latitude=45, elevation=160, area=30.0, throughput=10.0, precipitation_rate=12.0
    )
    assert cfg.rate_mm_h == 12.0


def test_rate_zero_when_no_source():
    cfg = ZoneCalcConfig(latitude=45, elevation=160)
    assert cfg.rate_mm_h == 0.0
    assert duration_seconds(5.0, cfg) == 0  # no rate -> no run


def test_duration_uses_precipitation_rate():
    # 24 mm/h direct, deficit 6 mm -> 6/24*3600 = 900 s
    cfg = ZoneCalcConfig(latitude=45, elevation=160, precipitation_rate=24.0)
    assert duration_seconds(6.0, cfg) == 900
