"""Sensor platform: one duration sensor per irrigation zone."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CROP_COEFFICIENT,
    ATTR_DEFICIT,
    ATTR_DELTA,
    ATTR_EVAPOTRANSPIRATION,
    ATTR_EXPLANATION,
    ATTR_LAST_CALCULATED,
    ATTR_LEAD_TIME,
    ATTR_MAXIMUM_DEFICIT,
    ATTR_MAXIMUM_DURATION,
    ATTR_MULTIPLIER,
    ATTR_NUMBER_OF_DATA_POINTS,
    ATTR_PRECIPITATION,
    ATTR_SIZE,
    ATTR_THROUGHPUT,
    ATTR_WINDOW_END,
    ATTR_WINDOW_START,
    DOMAIN,
)
from .coordinator import ETIrrigatorCoordinator

# Keys copied verbatim from coordinator data into the sensor's attributes.
_ATTR_KEYS = (
    ATTR_DEFICIT,
    ATTR_DELTA,
    ATTR_EVAPOTRANSPIRATION,
    ATTR_PRECIPITATION,
    ATTR_SIZE,
    ATTR_THROUGHPUT,
    ATTR_CROP_COEFFICIENT,
    ATTR_WINDOW_START,
    ATTR_WINDOW_END,
    ATTR_LAST_CALCULATED,
    ATTR_NUMBER_OF_DATA_POINTS,
    ATTR_MULTIPLIER,
    ATTR_LEAD_TIME,
    ATTR_MAXIMUM_DURATION,
    ATTR_MAXIMUM_DEFICIT,
    ATTR_EXPLANATION,
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the zone sensors from the coordinator."""
    coordinator: ETIrrigatorCoordinator = hass.data[DOMAIN]
    async_add_entities(
        ETIrrigatorZoneSensor(coordinator, zone.key, zone.name)
        for zone in coordinator.zones
    )


class ETIrrigatorZoneSensor(CoordinatorEntity[ETIrrigatorCoordinator], SensorEntity):
    """Recommended irrigation run-time (seconds) for one zone."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:sprinkler-variant"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ETIrrigatorCoordinator,
        zone_key: str,
        zone_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._zone_key = zone_key
        self._attr_name = f"ET Irrigator {zone_name}"
        self._attr_unique_id = f"{DOMAIN}_{zone_key}"
        self.entity_id = f"sensor.{DOMAIN}_{zone_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "et_irrigator")},
            name="ET Irrigator",
            manufacturer="ET Irrigator",
        )

    @property
    def _zone_data(self) -> dict | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._zone_key)

    @property
    def native_value(self) -> int | None:
        data = self._zone_data
        return data.get("duration") if data else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self._zone_data or {}
        return {key: data[key] for key in _ATTR_KEYS if key in data}

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
