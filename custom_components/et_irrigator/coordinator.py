"""Rolling-window irrigation coordinator."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .calc import ZoneCalcConfig, ZoneResult, compute_zone, compute_zone_hourly
from .const import DOMAIN, ET_METHOD_HOURLY
from .statistics import (
    async_fetch_statistics,
    async_last_irrigation_end,
    build_days,
    build_hours,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ZoneConfig:
    """Full per-zone configuration (HA-level)."""

    name: str
    calc: ZoneCalcConfig
    irrigation_sensor: str | None
    max_window_days: int

    @property
    def key(self) -> str:
        return slugify(self.name)


class ETIrrigatorCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Recomputes every zone's irrigation run-time from long-term statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        sensors: dict[str, str | None],
        wind_height: float,
        zones: list[ZoneConfig],
        et_method: str = ET_METHOD_HOURLY,
        longitude: float = 0.0,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Recompute is driven by the hourly-statistics event; this interval
            # is only a safety fallback so data never goes fully stale.
            update_interval=timedelta(hours=6),
        )
        self.sensors = sensors
        self.wind_height = wind_height
        self.zones = zones
        self.et_method = et_method
        self.longitude = longitude

    def update_config(
        self,
        *,
        sensors: dict[str, str | None],
        wind_height: float,
        zones: list[ZoneConfig],
        et_method: str,
        longitude: float,
    ) -> None:
        """Replace the config in place (for a YAML reload without restart).

        Mutating the existing coordinator keeps already-created entities bound to
        it; the caller reconciles added/removed zones and triggers a refresh.
        """
        self.sensors = sensors
        self.wind_height = wind_height
        self.zones = zones
        self.et_method = et_method
        self.longitude = longitude

    @property
    def weather_ids(self) -> set[str]:
        return {eid for eid in self.sensors.values() if eid}

    async def _async_update_data(self) -> dict[str, dict]:
        now = dt_util.utcnow()
        results: dict[str, dict] = {}
        for zone in self.zones:
            try:
                results[zone.key] = await self._calculate_zone(zone, now)
            except Exception as err:  # noqa: BLE001 - one bad zone must not kill the rest
                _LOGGER.exception("ET Irrigator: zone '%s' failed", zone.name)
                # Preserve last good value if we have one.
                if self.data and zone.key in self.data:
                    results[zone.key] = self.data[zone.key]
                else:
                    raise UpdateFailed(str(err)) from err
        return results

    async def _calculate_zone(self, zone: ZoneConfig, now) -> dict:
        window_start = now - timedelta(days=zone.max_window_days)
        if zone.irrigation_sensor:
            reference = await async_last_irrigation_end(
                self.hass, zone.irrigation_sensor, window_start, now
            )
        else:
            reference = window_start

        stats = await async_fetch_statistics(
            self.hass, self.weather_ids, reference, now
        )
        if self.et_method == ET_METHOD_HOURLY:
            hours = build_hours(
                stats, self.sensors, self.wind_height, self.longitude
            )
            result: ZoneResult = compute_zone_hourly(hours, zone.calc)
        else:
            days = build_days(stats, self.sensors, self.wind_height)
            result = compute_zone(days, zone.calc)

        return {
            "name": zone.name,
            "duration": result.duration,
            "deficit": result.deficit,
            "evapotranspiration": result.evapotranspiration,
            "precipitation": result.precipitation,
            "delta": result.delta,
            "number_of_data_points": result.number_of_data_points,
            "explanation": result.explanation,
            "window_start": reference.isoformat(),
            "window_end": now.isoformat(),
            "last_calculated": now.isoformat(),
            "size": zone.calc.area,
            "throughput": zone.calc.throughput,
            "crop_coefficient": zone.calc.crop_coefficient,
            "multiplier": zone.calc.multiplier,
            "lead_time": zone.calc.lead_time,
            "maximum_duration": zone.calc.maximum_duration,
            "maximum_deficit": zone.calc.maximum_deficit,
        }
