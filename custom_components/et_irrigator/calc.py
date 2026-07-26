"""Pure evapotranspiration / water-balance / run-time math.

This module has **no Home Assistant dependencies** so it can be unit-tested in
isolation and validated against the canonical FAO-56 worked examples. All input
aggregation from HA long-term statistics happens in ``statistics.py``; here we
consume already-aggregated weather (per day for the daily method, per hour for
the hourly method) and emit a per-zone result.

Units (metric, FAO-56 conventions):
  * temperatures        deg C
  * wind speed          m s-1
  * solar radiation     MJ m-2 per period (day for DayData, hour for HourData)
  * vapour pressure     kPa
  * precipitation / ET  mm
  * area                m2
  * throughput          L min-1
  * duration            seconds
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import pyeto
from .const import ALBEDO


@dataclass
class DayData:
    """Aggregated weather for one (possibly partial) day inside the window.

    A partial first/last day is represented by aggregating only the hourly
    statistics that fall inside the window: ``solar_rad_mj`` is the energy of the
    covered hours, ``t_min``/``t_max`` span the covered hours, and
    ``precipitation_mm`` is the rain that fell in them. There is therefore no
    separate day-fraction scaling — the partial coverage is already baked in.

    ``t_min``/``t_max`` may be ``None`` for a day that has rain statistics but no
    temperature statistics (recorder gap). Such a day contributes ETo = 0 while
    still contributing its rain — see :func:`run_water_balance`.
    """

    day_of_year: int
    t_min: float | None
    t_max: float | None
    solar_rad_mj: float
    wind_speed: float
    t_mean: float | None = None
    wind_height: float = 2.0
    dewpoint: float | None = None
    rh_min: float | None = None
    rh_max: float | None = None
    rh_mean: float | None = None
    precipitation_mm: float = 0.0

    @property
    def mean_temp(self) -> float | None:
        if self.t_mean is not None:
            return self.t_mean
        if self.t_min is None or self.t_max is None:
            return None
        return (self.t_min + self.t_max) / 2.0


@dataclass
class ZoneCalcConfig:
    """Static parameters needed to turn a water deficit into a run-time.

    The application rate [mm/h] is either given directly as
    ``precipitation_rate`` (e.g. measured with catch-cups) or derived from
    ``area`` + ``throughput``. ``precipitation_rate`` takes priority.
    """

    latitude: float  # degrees
    elevation: float  # metres
    area: float | None = None  # m2
    throughput: float | None = None  # L/min
    precipitation_rate: float | None = None  # mm/h (overrides area+throughput)
    crop_coefficient: float = 1.0
    # Total available water in the root zone [mm]: how much the soil can hold
    # between field capacity and the point we refuse to dry past. Doubles as the
    # ceiling of the depletion bucket, so it also bounds a single run-time.
    maximum_deficit: float = 30.0
    # Infiltration-rate ceiling [mm/h]. Rain arriving faster than this runs off
    # instead of entering the soil. None = no cap (all gauge rain infiltrates).
    max_infiltration_rate: float | None = None
    multiplier: float = 1.0
    lead_time: int = 0  # seconds
    maximum_duration: int = -1  # seconds, -1 = no cap

    @property
    def rate_mm_h(self) -> float:
        """Effective application rate [mm/h] — direct, else throughput/area."""
        if self.precipitation_rate is not None:
            return self.precipitation_rate
        if self.area and self.throughput:
            return self.throughput * 60.0 / self.area
        return 0.0


@dataclass
class WaterBalance:
    """Reconstruction of the FAO-56 soil water balance over the window.

    Sign convention: ``depletion`` and ``net_deficit`` are *deficits* — positive
    means the soil is dry, negative (only possible for ``net_deficit``) means a
    net surplus.
    """

    depletion: float  # mm below field capacity at window end, 0..taw
    net_deficit: float  # mm, sum(ETc - infiltration), unclamped; <0 = surplus
    drainage: float  # mm discarded by the field-capacity clamp (soil already full)
    runoff: float  # mm discarded by the infiltration-rate cap (rain too intense)
    capped: float  # mm discarded by the TAW clamp (chronic under-watering)
    evapotranspiration: float  # mm, sum of ETc
    precipitation: float  # mm, sum of gauge rain (gross, pre-cap)
    infiltration: float  # mm, rain that actually entered the soil


@dataclass
class ZoneResult:
    """Output of a single rolling-window calculation for a zone."""

    duration: int  # seconds
    deficit: float  # mm, the bucket depletion (0..maximum_deficit)
    evapotranspiration: float  # mm (sum over window, * Kc)
    precipitation: float  # mm (sum over window, gross gauge rain)
    delta: float  # mm, infiltration - evapotranspiration (negative = deficit)
    net_deficit: float  # mm, unclamped deficit (== -delta); positive = dry
    drainage: float  # mm of infiltrated rain lost past field capacity
    runoff: float  # mm of gauge rain lost to the infiltration-rate cap
    capped: float  # mm of ET the TAW ceiling refused to account for
    infiltration: float  # mm of gauge rain that entered the soil
    soil_moisture: float | None  # % of TAW still available, 0..100
    number_of_data_points: int
    explanation: str = ""
    daily_eto: list[float] = field(default_factory=list)


def run_water_balance(
    etc: Sequence[float],
    rain: Sequence[float],
    *,
    taw: float,
    step_hours: float = 1.0,
    max_infiltration_rate: float | None = None,
) -> WaterBalance:
    """Step-by-step FAO-56 soil water balance.

    ``etc`` (crop ET per step, mm) and ``rain`` (gauge rain per step, mm) are
    aligned by index. The depletion is clamped to ``[0, taw]`` at **every step**,
    which is what makes the result physical: rain arriving on an already-full soil
    drains away *when it falls* instead of retroactively cancelling the ET of the
    rest of the window.

    Each step is ``f(d) = clamp(d + etc - infiltration, 0, taw)`` — monotone and
    1-Lipschitz, and composition preserves both. So sliding the window by one step
    changes the answer by at most that step's net drying, and the memory of the
    ``depletion = 0`` initial condition is erased entirely the first time a later
    step hits a clamp. That is why the reconstruction is stable without persisting
    any state.
    """
    depletion = drainage = runoff = capped = 0.0
    net_deficit = 0.0
    eto_total = precip_total = infil_total = 0.0
    cap = max_infiltration_rate * step_hours if max_infiltration_rate else None

    for etc_step, rain_step in zip(etc, rain):
        infiltration = min(rain_step, cap) if cap is not None else rain_step
        runoff += rain_step - infiltration
        eto_total += etc_step
        precip_total += rain_step
        infil_total += infiltration

        net_deficit += etc_step - infiltration
        depletion += etc_step - infiltration
        if depletion < 0.0:
            drainage += -depletion
            depletion = 0.0
        elif depletion > taw:
            capped += depletion - taw
            depletion = taw

    return WaterBalance(
        depletion=depletion,
        net_deficit=net_deficit,
        drainage=drainage,
        runoff=runoff,
        capped=capped,
        evapotranspiration=eto_total,
        precipitation=precip_total,
        infiltration=infil_total,
    )


def _actual_vapour_pressure(day: DayData) -> float:
    """Actual vapour pressure [kPa], best available source (FAO-56 priority)."""
    if day.dewpoint is not None:
        return pyeto.avp_from_tdew(day.dewpoint)
    if day.rh_min is not None and day.rh_max is not None:
        return pyeto.avp_from_rhmin_rhmax(day.t_min, day.t_max, day.rh_min, day.rh_max)
    if day.rh_max is not None:
        return pyeto.avp_from_rhmax(day.t_min, day.rh_max)
    if day.rh_mean is not None:
        return pyeto.avp_from_rhmean(day.t_min, day.t_max, day.rh_mean)
    # Fallback: assume dewpoint == tmin (humid-climate FAO-56 approximation).
    return pyeto.avp_from_tmin(day.t_min)


def eto_fao56_day(day: DayData, latitude: float, elevation: float) -> float:
    """Reference ET (ETo) for one day [mm], FAO-56 Penman-Monteith.

    Uses *measured* solar radiation for the net-radiation term. For a partial
    first/last window day, ``day`` already holds only the covered hours' data.

    Returns 0.0 when the day has no temperature statistics: the water balance then
    credits that day's rain with no ET, which under-estimates ET rather than
    silently dropping the rain.
    """
    tmin, tmax = day.t_min, day.t_max
    tmean = day.mean_temp
    if tmin is None or tmax is None or tmean is None:
        return 0.0

    lat_rad = pyeto.deg2rad(latitude)

    svp = pyeto.mean_svp(tmin, tmax)
    avp = _actual_vapour_pressure(day)
    dsvp = pyeto.delta_svp(tmean)
    psy = pyeto.psy_const(pyeto.atm_pressure(elevation))
    ws2 = pyeto.wind_speed_2m(day.wind_speed, day.wind_height)

    sol_dec = pyeto.sol_dec(day.day_of_year)
    sha = pyeto.sunset_hour_angle(lat_rad, sol_dec)
    ird = pyeto.inv_rel_dist_earth_sun(day.day_of_year)
    extra_rad = pyeto.et_rad(lat_rad, sol_dec, sha, ird)
    clear_sky_rad = pyeto.cs_rad(elevation, extra_rad)

    ni_sw_rad = pyeto.net_in_sol_rad(day.solar_rad_mj, albedo=ALBEDO)
    # net_out_lw_rad applies the Stefan-Boltzmann law: temperatures MUST be in
    # Kelvin. Passing Celsius collapses the longwave term to ~0 and inflates ETo.
    no_lw_rad = pyeto.net_out_lw_rad(
        pyeto.celsius2kelvin(tmin),
        pyeto.celsius2kelvin(tmax),
        day.solar_rad_mj,
        clear_sky_rad,
        avp,
    )
    n_rad = pyeto.net_rad(ni_sw_rad, no_lw_rad)

    eto = pyeto.fao56_penman_monteith(
        n_rad, pyeto.celsius2kelvin(tmean), ws2, svp, avp, dsvp, psy
    )
    return max(0.0, eto)


def duration_seconds(deficit: float, cfg: ZoneCalcConfig) -> int:
    """Convert a water deficit [mm] into an irrigation run-time [seconds].

    duration = |deficit| / rate * 3600, where the application rate [mm/h] is
    ``cfg.rate_mm_h`` (given directly, or throughput[L/min] * 60 / area[m2];
    1 L applied over 1 m2 == 1 mm). Mirrors Smart Irrigation's formula.
    """
    rate_mm_h = cfg.rate_mm_h
    if deficit <= 0 or rate_mm_h <= 0:
        return 0
    seconds = deficit / rate_mm_h * 3600.0
    seconds *= cfg.multiplier
    if cfg.maximum_duration >= 0:
        seconds = min(seconds, float(cfg.maximum_duration))
    if seconds <= 0:
        return 0
    return round(cfg.lead_time + seconds)


def _balance_result(
    wb: WaterBalance,
    n_points: int,
    cfg: ZoneCalcConfig,
    window_label: str,
    per_step_eto: list[float],
) -> ZoneResult:
    """Turn a reconstructed water balance into the published zone result."""
    duration = duration_seconds(wb.depletion, cfg)
    taw = cfg.maximum_deficit
    soil_moisture = 100.0 * (1.0 - wb.depletion / taw) if taw > 0 else None

    explanation = (
        f"window={window_label} ETc={wb.evapotranspiration:.2f}mm "
        f"rain={wb.precipitation:.2f}mm (infiltrated {wb.infiltration:.2f}mm, "
        f"runoff {wb.runoff:.2f}mm, drained {wb.drainage:.2f}mm) "
        f"deficit={wb.depletion:.2f}/{taw:.2f}mm "
        f"rate={cfg.rate_mm_h:.2f}mm/h -> {duration}s"
    )
    if wb.capped > 0:
        explanation += f" [capped {wb.capped:.2f}mm at TAW]"

    return ZoneResult(
        duration=duration,
        deficit=round(wb.depletion, 3),
        evapotranspiration=round(wb.evapotranspiration, 3),
        precipitation=round(wb.precipitation, 3),
        delta=round(-wb.net_deficit, 3),
        net_deficit=round(wb.net_deficit, 3),
        drainage=round(wb.drainage, 3),
        runoff=round(wb.runoff, 3),
        capped=round(wb.capped, 3),
        infiltration=round(wb.infiltration, 3),
        soil_moisture=round(soil_moisture, 1) if soil_moisture is not None else None,
        number_of_data_points=n_points,
        explanation=explanation,
        daily_eto=[round(e, 3) for e in per_step_eto],
    )


def compute_zone(days: list[DayData], cfg: ZoneCalcConfig) -> ZoneResult:
    """Daily-method rolling-window calculation for one zone.

    ``days`` are the per-day aggregates spanning [last_irrigation, now], run
    through :func:`run_water_balance` one day at a time. The soil is assumed at
    field capacity (depletion 0) at the window start; this holds right after
    watering, and in the fallback case (no irrigation within max_window_days) the
    error is bounded and erased by the first clamp.

    Two approximations relative to the hourly method: rain and ET of the *same
    day* net out before the clamp, so drainage is under-counted; and the
    infiltration cap is applied as ``rate * 24h``, which almost never binds. The
    hourly method is the default and the recommended one.

    Pure function of ``days`` + ``cfg`` -> idempotent.
    """
    eto = [eto_fao56_day(d, cfg.latitude, cfg.elevation) for d in days]
    wb = run_water_balance(
        [e * cfg.crop_coefficient for e in eto],
        [d.precipitation_mm for d in days],
        taw=cfg.maximum_deficit,
        step_hours=24.0,
        max_infiltration_rate=cfg.max_infiltration_rate,
    )
    return _balance_result(wb, len(days), cfg, f"{len(days)}d", eto)


# ---------------------------------------------------------------------------
# Hourly FAO-56 (Penman-Monteith, Eq. 53) — preferred for the rolling window.
#
# Computing ET per hour (our data granularity) instead of per calendar day
# removes the partial-day approximation and makes the integrated deficit
# monotonic between irrigations. The daily equation is NOT just this / 24:
# the hourly form uses different constants (37 vs 900), a day/night wind
# coefficient Cd (0.24 / 0.96) and a non-negligible soil heat flux G
# (0.1*Rn day, 0.5*Rn night). pyeto only ships the daily equation, so the
# assembly below is ours; the sub-terms reuse pyeto where the maths are
# period-agnostic (svp, avp, psy, wind, net shortwave, clear-sky).
# ---------------------------------------------------------------------------


@dataclass
class HourData:
    """Aggregated weather for one clock hour inside the rolling window."""

    day_of_year: int
    solar_time_hours: float  # solar time at the hour midpoint (for the hour angle)
    # Mean hourly temperature [deg C], or None for an hour with rain statistics but
    # no temperature statistics (recorder gap) -> ETo 0, rain still counted.
    t: float | None
    solar_rad_mj: float  # hourly solar radiation total, MJ m-2 h-1
    wind_speed: float  # m s-1
    wind_height: float = 2.0
    dewpoint: float | None = None
    precipitation_mm: float = 0.0


def ra_hourly(latitude: float, day_of_year: int, solar_time_hours: float,
              period_h: float = 1.0) -> float:
    """Extraterrestrial radiation for one hour [MJ m-2 h-1] — FAO-56 Eq. 28.

    Clamped to 0 at night (sun below horizon).
    """
    phi = pyeto.deg2rad(latitude)
    dr = pyeto.inv_rel_dist_earth_sun(day_of_year)
    dec = pyeto.sol_dec(day_of_year)
    w = math.radians((solar_time_hours - 12.0) * 15.0)  # hour angle at midpoint
    w1 = w - math.pi * period_h / 24.0
    w2 = w + math.pi * period_h / 24.0
    ra = ((12 * 60 / math.pi) * pyeto.SOLAR_CONSTANT * dr * (
        (w2 - w1) * math.sin(phi) * math.sin(dec)
        + math.cos(phi) * math.cos(dec) * (math.sin(w2) - math.sin(w1))
    ))
    return max(0.0, ra)


def _avp_hourly(hour: HourData) -> float:
    """Actual vapour pressure [kPa] for the hour (dewpoint preferred)."""
    if hour.dewpoint is not None:
        return pyeto.avp_from_tdew(hour.dewpoint)
    # Degraded fallback (no humidity input): assume saturation -> kills the
    # aerodynamic term, ET from radiation only. Dewpoint is recommended.
    return pyeto.svp_from_t(hour.t)


def eto_fao56_hourly(hour: HourData, latitude: float, elevation: float) -> float:
    """Reference ET for one hour [mm h-1], FAO-56 Penman-Monteith (Eq. 53).

    Returns 0.0 for an hour with no temperature statistics, so that the hour's rain
    still reaches the water balance (under-estimating ET is the safe direction).
    """
    t = hour.t
    if t is None:
        return 0.0
    delta = pyeto.delta_svp(t)
    psy = pyeto.psy_const(pyeto.atm_pressure(elevation))
    es = pyeto.svp_from_t(t)
    ea = _avp_hourly(hour)
    u2 = pyeto.wind_speed_2m(hour.wind_speed, hour.wind_height)

    ra = ra_hourly(latitude, hour.day_of_year, hour.solar_time_hours)
    rso = pyeto.cs_rad(elevation, ra) if ra > 0 else 0.0
    ni_sw = pyeto.net_in_sol_rad(hour.solar_rad_mj, albedo=ALBEDO)
    t_k = pyeto.celsius2kelvin(t)
    if rso > 0:
        # Reuse the daily longwave with tmin=tmax=T_hr (so (T^4+T^4)/2 = T^4)
        # and /24 to turn the daily Stefan-Boltzmann constant into the hourly one.
        no_lw = pyeto.net_out_lw_rad(t_k, t_k, hour.solar_rad_mj, rso, ea) / 24.0
    else:
        # Night: Rs/Rso is undefined; use a representative cloudiness factor.
        sigma_hr = pyeto.STEFAN_BOLTZMANN_CONSTANT / 24.0
        no_lw = sigma_hr * t_k**4 * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * 0.4 - 0.35)
    rn = ni_sw - no_lw

    g = 0.1 * rn if rn > 0 else 0.5 * rn  # soil heat flux, day / night
    cd = 0.24 if rn > 0 else 0.96
    num = 0.408 * delta * (rn - g) + psy * (37.0 / (t + 273.0)) * u2 * (es - ea)
    den = delta + psy * (1 + cd * u2)
    return max(0.0, num / den)


def compute_zone_hourly(hours: list[HourData], cfg: ZoneCalcConfig) -> ZoneResult:
    """Hourly-method rolling-window calculation for one zone.

    Runs the soil water balance one hour at a time. Because each completed hour's
    data is final, the deficit is monotonic between irrigations during dry spells
    (no daily re-aggregation wobble), and rain is drained against the soil state of
    the hour it fell in. Pure function of ``hours`` + ``cfg`` -> idempotent.
    """
    eto = [eto_fao56_hourly(h, cfg.latitude, cfg.elevation) for h in hours]
    wb = run_water_balance(
        [e * cfg.crop_coefficient for e in eto],
        [h.precipitation_mm for h in hours],
        taw=cfg.maximum_deficit,
        step_hours=1.0,
        max_infiltration_rate=cfg.max_infiltration_rate,
    )
    return _balance_result(wb, len(hours), cfg, f"{len(hours)}h", eto)
