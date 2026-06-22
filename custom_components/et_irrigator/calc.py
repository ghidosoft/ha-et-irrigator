"""Pure evapotranspiration / water-balance / run-time math.

This module has **no Home Assistant dependencies** so it can be unit-tested in
isolation and validated against the canonical FAO-56 worked examples. All input
aggregation from HA long-term statistics happens in ``statistics.py``; here we
only consume already-aggregated daily weather and emit a per-zone result.

Units (metric, FAO-56 conventions):
  * temperatures        deg C
  * wind speed          m s-1
  * solar radiation     MJ m-2 day-1 (daily total, full-day equivalent)
  * vapour pressure     kPa
  * precipitation / ET  mm
  * area                m2
  * throughput          L min-1
  * duration            seconds
"""

from __future__ import annotations

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
    """Static parameters needed to turn a water deficit into a run-time."""

    latitude: float  # degrees
    elevation: float  # metres
    area: float  # m2
    throughput: float  # L/min
    crop_coefficient: float = 1.0
    maximum_deficit: float = 30.0  # mm (field capacity cap)
    multiplier: float = 1.0
    lead_time: int = 0  # seconds
    maximum_duration: int = -1  # seconds, -1 = no cap


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

    duration = |deficit| / precipitation_rate * 3600, where
    precipitation_rate [mm/h] = throughput[L/min] * 60 / area[m2]
    (1 L applied over 1 m2 == 1 mm). Mirrors Smart Irrigation's formula.
    """
    if deficit <= 0 or cfg.throughput <= 0 or cfg.area <= 0:
        return 0
    rate_mm_h = cfg.throughput * 60.0 / cfg.area
    seconds = deficit / rate_mm_h * 3600.0
    seconds *= cfg.multiplier
    if cfg.maximum_duration >= 0:
        seconds = min(seconds, float(cfg.maximum_duration))
    if seconds <= 0:
        return 0
    return round(cfg.lead_time + seconds)


def compute_zone(days: list[DayData], cfg: ZoneCalcConfig) -> ZoneResult:
    """Run the rolling-window calculation for one zone.

    ``days`` are the per-day aggregates spanning [last_irrigation, now]. The soil
    is assumed at field capacity (deficit 0) at the window start; this holds right
    after watering, but in the fallback case (no irrigation within max_window_days)
    it is an assumption mitigated only by ``maximum_deficit``. The result is a pure
    function of ``days`` + ``cfg`` -> idempotent.
    """
    daily_eto: list[float] = []
    eto_total = 0.0
    precip_total = 0.0
    for day in days:
        eto = eto_fao56_day(day, cfg.latitude, cfg.elevation)
        daily_eto.append(eto)
        eto_total += eto
        precip_total += day.precipitation_mm

    eto_crop = eto_total * cfg.crop_coefficient
    delta = precip_total - eto_crop
    deficit = min(cfg.maximum_deficit, max(0.0, -delta))
    duration = duration_seconds(deficit, cfg)

    explanation = (
        f"window={len(days)}d ETo*Kc={eto_crop:.2f}mm precip={precip_total:.2f}mm "
        f"deficit={deficit:.2f}mm rate="
        f"{(cfg.throughput * 60.0 / cfg.area) if cfg.area else 0:.2f}mm/h "
        f"-> {duration}s"
    )

    return ZoneResult(
        duration=duration,
        deficit=round(deficit, 3),
        evapotranspiration=round(eto_crop, 3),
        precipitation=round(precip_total, 3),
        delta=round(delta, 3),
        number_of_data_points=len(days),
        explanation=explanation,
        daily_eto=[round(e, 3) for e in daily_eto],
    )
