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
    """

    day_of_year: int
    t_min: float
    t_max: float
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
    def mean_temp(self) -> float:
        if self.t_mean is not None:
            return self.t_mean
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
    maximum_deficit: float = 30.0  # mm (field capacity cap)
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
class ZoneResult:
    """Output of a single rolling-window calculation for a zone."""

    duration: int  # seconds
    deficit: float  # mm
    evapotranspiration: float  # mm (sum over window, * Kc)
    precipitation: float  # mm (sum over window)
    delta: float  # mm, precipitation - evapotranspiration (negative = deficit)
    number_of_data_points: int
    explanation: str = ""
    daily_eto: list[float] = field(default_factory=list)


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
    """
    lat_rad = pyeto.deg2rad(latitude)
    tmin, tmax = day.t_min, day.t_max
    tmean = day.mean_temp

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
    eto_total: float,
    precip_total: float,
    n_points: int,
    cfg: ZoneCalcConfig,
    window_label: str,
    per_step_eto: list[float],
) -> ZoneResult:
    """Turn an integrated ETo + precipitation over the window into a result."""
    eto_crop = eto_total * cfg.crop_coefficient
    delta = precip_total - eto_crop
    deficit = min(cfg.maximum_deficit, max(0.0, -delta))
    duration = duration_seconds(deficit, cfg)

    explanation = (
        f"window={window_label} ETo*Kc={eto_crop:.2f}mm precip={precip_total:.2f}mm "
        f"deficit={deficit:.2f}mm rate={cfg.rate_mm_h:.2f}mm/h "
        f"-> {duration}s"
    )
    return ZoneResult(
        duration=duration,
        deficit=round(deficit, 3),
        evapotranspiration=round(eto_crop, 3),
        precipitation=round(precip_total, 3),
        delta=round(delta, 3),
        number_of_data_points=n_points,
        explanation=explanation,
        daily_eto=[round(e, 3) for e in per_step_eto],
    )


def compute_zone(days: list[DayData], cfg: ZoneCalcConfig) -> ZoneResult:
    """Daily-method rolling-window calculation for one zone.

    ``days`` are the per-day aggregates spanning [last_irrigation, now]. The soil
    is assumed at field capacity (deficit 0) at the window start; this holds right
    after watering, but in the fallback case (no irrigation within max_window_days)
    it is an assumption mitigated only by ``maximum_deficit``. Pure function of
    ``days`` + ``cfg`` -> idempotent.
    """
    eto = [eto_fao56_day(d, cfg.latitude, cfg.elevation) for d in days]
    return _balance_result(
        sum(eto), sum(d.precipitation_mm for d in days), len(days), cfg,
        f"{len(days)}d", eto,
    )


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
    t: float  # mean hourly temperature, deg C
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
    """Reference ET for one hour [mm h-1], FAO-56 Penman-Monteith (Eq. 53)."""
    t = hour.t
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

    Sums per-hour ETo over the window. Because each completed hour's data is
    final, the integrated deficit is monotonic between irrigations (no daily
    re-aggregation wobble). Pure function of ``hours`` + ``cfg`` -> idempotent.
    """
    eto = [eto_fao56_hourly(h, cfg.latitude, cfg.elevation) for h in hours]
    return _balance_result(
        sum(eto), sum(h.precipitation_mm for h in hours), len(hours), cfg,
        f"{len(hours)}h", eto,
    )
