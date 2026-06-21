"""Vendored subset of PyETo (FAO-56 evapotranspiration).

Only the pure-Python ``fao``/``convert``/``_check`` modules are vendored so the
integration needs no external requirements beyond numpy (shipped with Home
Assistant). The convenience pandas-based wrapper of upstream ``aquacropeto`` is
deliberately omitted.

Upstream: https://github.com/woodcrafty/PyETo (Mark Richards) /
https://github.com/aquacropos/aquacrop-eto
License: BSD 3-Clause (see LICENSE in this directory).
"""

from .convert import celsius2kelvin, deg2rad, kelvin2celsius, rad2deg
from .fao import (
    SOLAR_CONSTANT,
    STEFAN_BOLTZMANN_CONSTANT,
    atm_pressure,
    avp_from_rhmax,
    avp_from_rhmean,
    avp_from_rhmin_rhmax,
    avp_from_tdew,
    avp_from_tmin,
    cs_rad,
    daily_mean_t,
    daylight_hours,
    delta_svp,
    energy2evap,
    et_rad,
    fao56_penman_monteith,
    hargreaves,
    inv_rel_dist_earth_sun,
    mean_svp,
    net_in_sol_rad,
    net_out_lw_rad,
    net_rad,
    psy_const,
    rh_from_avp_svp,
    sol_dec,
    sol_rad_from_sun_hours,
    sol_rad_from_t,
    sunset_hour_angle,
    svp_from_t,
    wind_speed_2m,
)

__all__ = [
    "SOLAR_CONSTANT",
    "STEFAN_BOLTZMANN_CONSTANT",
    "atm_pressure",
    "avp_from_rhmax",
    "avp_from_rhmean",
    "avp_from_rhmin_rhmax",
    "avp_from_tdew",
    "avp_from_tmin",
    "celsius2kelvin",
    "cs_rad",
    "daily_mean_t",
    "daylight_hours",
    "deg2rad",
    "delta_svp",
    "energy2evap",
    "et_rad",
    "fao56_penman_monteith",
    "hargreaves",
    "inv_rel_dist_earth_sun",
    "kelvin2celsius",
    "mean_svp",
    "net_in_sol_rad",
    "net_out_lw_rad",
    "net_rad",
    "psy_const",
    "rad2deg",
    "rh_from_avp_svp",
    "sol_dec",
    "sol_rad_from_sun_hours",
    "sol_rad_from_t",
    "sunset_hour_angle",
    "svp_from_t",
    "wind_speed_2m",
]
