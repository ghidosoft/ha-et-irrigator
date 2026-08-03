"""Rolling-window irrigation coordinator."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .calc import (
    HourData,
    ZoneCalcConfig,
    ZoneResult,
    compute_zone,
    compute_zone_hourly,
    step_cap,
    step_infiltration,
)
from .const import (
    DOMAIN,
    ET_METHOD_HOURLY,
    SUFFIX_HOURLY_DRAINAGE,
    SUFFIX_HOURLY_RAIN,
    SUFFIX_HOURLY_RUNOFF,
    UNIT_MM,
)
from .statistics import (
    async_export_hourly_series,
    async_fetch_statistics,
    async_last_irrigation_end,
    build_days,
    build_hours,
    slice_stats,
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
        # Rewrite the whole exported window on the next refresh. Set at setup and
        # whenever the config changes, because that is the only thing that can
        # change an already-published hour; the hourly refresh only appends.
        self._full_export = True

    def request_full_export(self) -> None:
        """Ask the next refresh to rewrite the exported window, not just append."""
        self._full_export = True

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
        self._full_export = True

    @property
    def weather_ids(self) -> set[str]:
        return {eid for eid in self.sensors.values() if eid}

    async def _async_update_data(self) -> dict[str, dict]:
        now = dt_util.utcnow()
        # Consumed once per cycle, not per zone.
        rewrite = self._full_export
        self._full_export = False
        results: dict[str, dict] = {}
        for zone in self.zones:
            try:
                results[zone.key] = await self._calculate_zone(zone, now, rewrite)
            except Exception as err:  # noqa: BLE001 - one bad zone must not kill the rest
                _LOGGER.exception("ET Irrigator: zone '%s' failed", zone.name)
                # Preserve last good value if we have one.
                if self.data and zone.key in self.data:
                    results[zone.key] = self.data[zone.key]
                else:
                    raise UpdateFailed(str(err)) from err
        return results

    async def _calculate_zone(
        self, zone: ZoneConfig, now: datetime, rewrite: bool
    ) -> dict:
        window_start = now - timedelta(days=zone.max_window_days)
        if zone.irrigation_sensor:
            reference = await async_last_irrigation_end(
                self.hass, zone.irrigation_sensor, window_start, now
            )
        else:
            reference = window_start

        # Fetched over the whole export window; the water balance still only sees
        # [reference, now], otherwise the per-hour series would restart from
        # scratch after every irrigation.
        stats = await async_fetch_statistics(
            self.hass, self.weather_ids, window_start, now
        )
        export_hours: list[HourData] = []
        if self.et_method == ET_METHOD_HOURLY:
            export_hours = build_hours(
                stats, self.sensors, self.wind_height, self.longitude
            )
            # An hour with no timestamp cannot be placed in the window, but it is
            # kept rather than dropped: build_hours always sets one, and silently
            # losing an hour's rain would be the far worse failure. It simply
            # takes no part in the export.
            hours = [
                h for h in export_hours if h.start is None or h.start >= reference
            ]
            result: ZoneResult = compute_zone_hourly(hours, zone.calc)
        else:
            days = build_days(
                slice_stats(stats, reference), self.sensors, self.wind_height
            )
            hours = []
            result = compute_zone(days, zone.calc)

        export_through = await self._async_export_zone(
            zone, now, reference, export_hours, hours, result, rewrite
        )

        return {
            "name": zone.name,
            "duration": result.duration,
            "deficit": result.deficit,
            "evapotranspiration": result.evapotranspiration,
            "precipitation": result.precipitation,
            "infiltration": result.infiltration,
            "drainage": result.drainage,
            "runoff": result.runoff,
            "rain_lost": round(result.drainage + result.runoff, 3),
            "capped": result.capped,
            "soil_moisture": result.soil_moisture,
            "delta": result.delta,
            "net_deficit": result.net_deficit,
            "number_of_data_points": result.number_of_data_points,
            "explanation": result.explanation,
            "window_start": reference.isoformat(),
            "window_end": now.isoformat(),
            "last_calculated": now.isoformat(),
            "hourly_export_through": (
                export_through.isoformat() if export_through else None
            ),
            "size": zone.calc.area,
            "throughput": zone.calc.throughput,
            "rate": round(zone.calc.rate_mm_h, 3),
            "crop_coefficient": zone.calc.crop_coefficient,
            "multiplier": zone.calc.multiplier,
            "lead_time": zone.calc.lead_time,
            "maximum_duration": zone.calc.maximum_duration,
            "maximum_deficit": zone.calc.maximum_deficit,
            "max_infiltration_rate": zone.calc.max_infiltration_rate,
        }

    async def _async_export_zone(
        self,
        zone: ZoneConfig,
        now: datetime,
        reference: datetime,
        export_hours: list[HourData],
        balance_hours: list[HourData],
        result: ZoneResult,
        rewrite: bool,
    ) -> datetime | None:
        """Publish the zone's per-hour rain and losses as long-term statistics.

        Deliberately isolated: a failure here must not cost the zone its run-time,
        which is what the user's automation actually depends on.

        Only the hourly method is exported — under the daily method a "step" is a
        day, so there is no per-hour figure to publish. And with no rain sensor
        there is nothing to decompose.
        """
        if self.et_method != ET_METHOD_HOURLY or not self.sensors.get("rain"):
            return None

        try:
            return await self._async_export_series(
                zone, now, reference, export_hours, balance_hours, result, rewrite
            )
        except Exception:  # noqa: BLE001 - never let the export break the run-time
            _LOGGER.exception(
                "ET Irrigator: hourly statistics export failed for zone '%s'",
                zone.name,
            )
            # Re-arm the request so a failed rewrite is retried next cycle rather
            # than silently downgraded to an append.
            self._full_export = self._full_export or rewrite
            return None

    async def _async_export_series(
        self,
        zone: ZoneConfig,
        now: datetime,
        reference: datetime,
        export_hours: list[HourData],
        balance_hours: list[HourData],
        result: ZoneResult,
        rewrite: bool,
    ) -> datetime | None:
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        prefix = f"sensor.{DOMAIN}_{zone.key}"

        # Rain and runoff are pure functions of the hour's mm and the cap, so they
        # are the same no matter where the window starts: exported over the whole
        # window, and safe to rewrite in place.
        cap = step_cap(zone.calc.max_infiltration_rate, 1.0)
        rain = [(h.start, h.precipitation_mm) for h in export_hours if h.start]
        runoff = [
            (h.start, h.precipitation_mm - step_infiltration(h.precipitation_mm, cap))
            for h in export_hours
            if h.start
        ]

        # Drainage is path dependent — it follows the bucket's trajectory, hence
        # the window start — so it only exists inside the balance and is written
        # once, never revised. Aligned by timestamp, not by index: result.steps
        # matches the balance hours, which are the tail of export_hours, and the
        # hours themselves need not be contiguous.
        steps = {h.start: s for h, s in zip(balance_hours, result.steps)}
        drainage = [(h.start, steps[h.start].drainage) for h in balance_hours if h.start]
        # The hour an irrigation *ended* in falls before `reference` and is thus
        # excluded from the balance. Its drainage is unknowable to the model, so
        # it is recorded as 0.0 — once per irrigation, and never rewritten.
        boundary = reference.replace(minute=0, second=0, microsecond=0)
        if boundary < reference:
            drainage.insert(0, (boundary, 0.0))

        through = await async_export_hourly_series(
            self.hass,
            f"{prefix}_{SUFFIX_HOURLY_RAIN}",
            UNIT_MM,
            rain,
            cutoff=cutoff,
            rewrite=rewrite,
        )
        await async_export_hourly_series(
            self.hass,
            f"{prefix}_{SUFFIX_HOURLY_RUNOFF}",
            UNIT_MM,
            runoff,
            cutoff=cutoff,
            rewrite=rewrite,
        )
        await async_export_hourly_series(
            self.hass,
            f"{prefix}_{SUFFIX_HOURLY_DRAINAGE}",
            UNIT_MM,
            drainage,
            cutoff=cutoff,
            rewrite=False,  # path dependent: append-only, always
        )
        return through
