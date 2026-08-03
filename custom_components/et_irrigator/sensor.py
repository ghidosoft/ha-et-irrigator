"""Sensor platform: the run-time sensor plus diagnostic sensors, per zone.

Each zone publishes one *primary* sensor — the recommended run-time in seconds,
carrying the full attribute set — and a handful of **diagnostic** sensors in mm/%
that expose the water balance itself. The diagnostics exist because state
attributes get no long-term statistics: to graph the deficit natively in Home
Assistant it has to be a state, not an attribute.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CAPPED,
    ATTR_CROP_COEFFICIENT,
    ATTR_DEFICIT,
    ATTR_DELTA,
    ATTR_DRAINAGE,
    ATTR_EVAPOTRANSPIRATION,
    ATTR_EXPLANATION,
    ATTR_HOURLY_EXPORT_THROUGH,
    ATTR_INFILTRATION,
    ATTR_LAST_CALCULATED,
    ATTR_LEAD_TIME,
    ATTR_MAX_INFILTRATION_RATE,
    ATTR_MAXIMUM_DEFICIT,
    ATTR_MAXIMUM_DURATION,
    ATTR_MULTIPLIER,
    ATTR_NET_DEFICIT,
    ATTR_NUMBER_OF_DATA_POINTS,
    ATTR_PRECIPITATION,
    ATTR_RAIN_LOST,
    ATTR_RATE,
    ATTR_RUNOFF,
    ATTR_SIZE,
    ATTR_SOIL_MOISTURE,
    ATTR_THROUGHPUT,
    ATTR_WINDOW_END,
    ATTR_WINDOW_START,
    DOMAIN,
    HOURLY_SUFFIXES,
    SUFFIX_HOURLY_DRAINAGE,
    SUFFIX_HOURLY_RAIN,
    SUFFIX_HOURLY_RUNOFF,
    UNIT_MM,
)
from .coordinator import ETIrrigatorCoordinator
from .statistics import async_clear_hourly_series

# hass.data key holding the platform's add-callback and current entities, so a
# YAML reload can reconcile zone entities without a restart.
DATA_SENSOR_STORE = f"{DOMAIN}_sensor_store"

# Keys copied verbatim from coordinator data into the primary sensor's attributes.
_ATTR_KEYS = (
    ATTR_DEFICIT,
    ATTR_NET_DEFICIT,
    ATTR_DELTA,
    ATTR_EVAPOTRANSPIRATION,
    ATTR_PRECIPITATION,
    ATTR_INFILTRATION,
    ATTR_DRAINAGE,
    ATTR_RUNOFF,
    ATTR_RAIN_LOST,
    ATTR_CAPPED,
    ATTR_SOIL_MOISTURE,
    ATTR_SIZE,
    ATTR_THROUGHPUT,
    ATTR_RATE,
    ATTR_CROP_COEFFICIENT,
    ATTR_WINDOW_START,
    ATTR_WINDOW_END,
    ATTR_LAST_CALCULATED,
    ATTR_HOURLY_EXPORT_THROUGH,
    ATTR_NUMBER_OF_DATA_POINTS,
    ATTR_MULTIPLIER,
    ATTR_LEAD_TIME,
    ATTR_MAXIMUM_DURATION,
    ATTR_MAXIMUM_DEFICIT,
    ATTR_MAX_INFILTRATION_RATE,
    ATTR_EXPLANATION,
)


@dataclass(frozen=True)
class ZoneSensorSpec:
    """One sensor entity per zone, described declaratively."""

    suffix: str  # "" == the primary run-time sensor
    label: str  # appended to the zone name; "" for the primary sensor
    # Key to read from the coordinator's per-zone dict, or None for a sensor that
    # deliberately has no state (see the hourly-export specs below).
    data_key: str | None
    unit: str
    icon: str
    device_class: SensorDeviceClass | None = None
    precision: int | None = None
    diagnostic: bool = True
    with_attributes: bool = False
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT


# Most specs use state_class MEASUREMENT — that (not entity_category) is what
# makes Home Assistant record long-term statistics, so the diagnostic sensors are
# graphable in the native History/Statistics cards.
#
# Deliberately NOT TOTAL_INCREASING for the mm sums: they are sums over a *sliding*
# window, so they fall when old data scrolls out and HA would fabricate resets.
# Deliberately no SensorDeviceClass.PRECIPITATION either: its unit conversion is
# meaningless for a soil deficit and ambiguous for a signed value.
#
# The three `hourly_*` specs are the exception, with state_class None and no
# data_key at all; see the block above them for why both are required.
ZONE_SENSORS: tuple[ZoneSensorSpec, ...] = (
    ZoneSensorSpec(
        suffix="",
        label="",
        data_key="duration",
        unit=UnitOfTime.SECONDS,
        icon="mdi:sprinkler-variant",
        device_class=SensorDeviceClass.DURATION,
        diagnostic=False,
        with_attributes=True,
    ),
    ZoneSensorSpec(
        suffix="deficit",
        label="Deficit",
        data_key="deficit",
        unit=UNIT_MM,
        icon="mdi:water-minus",
        precision=2,
    ),
    ZoneSensorSpec(
        # Signed: positive = soil dry, negative = surplus rain that was discarded.
        # This is the unclamped window sum, so it still shows window-edge steps by
        # construction — it is a diagnostic, not the number to irrigate from.
        suffix="net_deficit",
        label="Net deficit",
        data_key="net_deficit",
        unit=UNIT_MM,
        icon="mdi:swap-vertical",
        precision=2,
    ),
    ZoneSensorSpec(
        suffix="soil_moisture",
        label="Soil moisture",
        data_key="soil_moisture",
        unit=PERCENTAGE,
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.MOISTURE,
        precision=1,
    ),
    ZoneSensorSpec(
        suffix="evapotranspiration",
        label="Evapotranspiration",
        data_key="evapotranspiration",
        unit=UNIT_MM,
        icon="mdi:weather-sunny",
        precision=2,
    ),
    ZoneSensorSpec(
        suffix="precipitation",
        label="Precipitation",
        data_key="precipitation",
        unit=UNIT_MM,
        icon="mdi:weather-rainy",
        precision=2,
    ),
    ZoneSensorSpec(
        suffix="rain_lost",
        label="Rain lost",
        data_key="rain_lost",
        unit=UNIT_MM,
        icon="mdi:water-off",
        precision=2,
    ),
    # --- Per-hour series, published as statistics rather than as states -----
    #
    # These three carry no state at all. They exist only as anchors:
    # `async_import_statistics` requires a valid entity_id, and chart cards
    # resolve `hass.states[entity]` before they will render a series at all.
    # The values live in the recorder's statistics table, written by the
    # coordinator with each hour's true timestamp, and are read back as
    # `change`. See statistics.py for why a state could not do this job.
    #
    # state_class MUST stay None, and native_value MUST stay non-numeric:
    #   * with a state_class, the recorder would compile its own statistics for
    #     the same id (has_mean=True) and fight our import (has_sum=True),
    #     rewriting the metadata row against each other every hour;
    #   * with a numeric state and no state_class, sensor/recorder.py raises the
    #     `state_class_removed` repair, which is not fixable and would sit in
    #     Repairs forever.
    ZoneSensorSpec(
        suffix=SUFFIX_HOURLY_RAIN,
        label="Hourly rain",
        data_key=None,
        unit=UNIT_MM,
        icon="mdi:weather-pouring",
        state_class=None,
    ),
    ZoneSensorSpec(
        suffix=SUFFIX_HOURLY_RUNOFF,
        label="Hourly runoff",
        data_key=None,
        unit=UNIT_MM,
        icon="mdi:water-alert",
        state_class=None,
    ),
    ZoneSensorSpec(
        suffix=SUFFIX_HOURLY_DRAINAGE,
        label="Hourly drainage",
        data_key=None,
        unit=UNIT_MM,
        icon="mdi:water-arrow-down",
        state_class=None,
    ),
)


def _build_zone_entities(
    coordinator: ETIrrigatorCoordinator, zone_key: str, zone_name: str
) -> list[ETIrrigatorZoneSensor]:
    return [
        ETIrrigatorZoneSensor(coordinator, zone_key, zone_name, spec)
        for spec in ZONE_SENSORS
    ]


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the zone sensors from the coordinator."""
    coordinator: ETIrrigatorCoordinator = hass.data[DOMAIN]
    entities = {
        zone.key: _build_zone_entities(coordinator, zone.key, zone.name)
        for zone in coordinator.zones
    }
    async_add_entities(
        [entity for group in entities.values() for entity in group]
    )
    hass.data[DATA_SENSOR_STORE] = {"add": async_add_entities, "entities": entities}


async def async_reload_entities(hass: HomeAssistant) -> None:
    """Reconcile zone entities against the (already-reloaded) coordinator.

    The coordinator object is reused, so existing entities for unchanged zones
    keep working; only removed zones are dropped and new zones are added. Each
    zone owns a *list* of entities, so a dropped zone takes all of them with it.
    """
    store = hass.data.get(DATA_SENSOR_STORE)
    if not store:
        return
    coordinator: ETIrrigatorCoordinator = hass.data[DOMAIN]
    current: dict[str, list[ETIrrigatorZoneSensor]] = store["entities"]
    new_zones = {zone.key: zone for zone in coordinator.zones}

    registry = er.async_get(hass)
    orphaned: list[str] = []
    for key in list(current):
        if key not in new_zones:
            for entity in current.pop(key):
                entity_id = entity.entity_id
                await entity.async_remove()
                # Purge the registry entry too, so a dropped zone disappears
                # instead of lingering as a restored `unavailable` state.
                if registry.async_get(entity_id):
                    registry.async_remove(entity_id)
            # The imported statistics outlive the entity, so they have to go too:
            # otherwise `validate_statistics` reports them as `no_state` forever.
            orphaned += [f"sensor.{DOMAIN}_{key}_{suffix}" for suffix in HOURLY_SUFFIXES]
    await async_clear_hourly_series(hass, orphaned)

    added = {
        key: _build_zone_entities(coordinator, zone.key, zone.name)
        for key, zone in new_zones.items()
        if key not in current
    }
    if added:
        store["add"]([entity for group in added.values() for entity in group])
        current.update(added)


class ETIrrigatorZoneSensor(CoordinatorEntity[ETIrrigatorCoordinator], SensorEntity):
    """One published value of a zone's water balance."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: ETIrrigatorCoordinator,
        zone_key: str,
        zone_name: str,
        spec: ZoneSensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self._zone_key = zone_key
        self._spec = spec
        suffix = f"_{spec.suffix}" if spec.suffix else ""
        label = f" {spec.label}" if spec.label else ""
        self._attr_name = f"ET Irrigator {zone_name}{label}"
        self._attr_unique_id = f"{DOMAIN}_{zone_key}{suffix}"
        self.entity_id = f"sensor.{DOMAIN}_{zone_key}{suffix}"
        self._attr_icon = spec.icon
        self._attr_state_class = spec.state_class
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_suggested_display_precision = spec.precision
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "et_irrigator")},
            name="ET Irrigator",
            manufacturer="ET Irrigator",
        )

    @property
    def zone_key(self) -> str:
        return self._zone_key

    @property
    def _zone_data(self) -> dict | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._zone_key)

    @property
    def native_value(self) -> float | int | None:
        # None (-> state "unknown") is the *point* for the hourly-export specs,
        # not a missing feature. They exist only so that a valid entity_id and a
        # hass.states entry back the imported statistics; a numeric state here
        # would trip the non-fixable `state_class_removed` repair, because their
        # state_class is deliberately None.
        if self._spec.data_key is None:
            return None
        data = self._zone_data
        return data.get(self._spec.data_key) if data else None

    @property
    def extra_state_attributes(self) -> dict:
        # Only the primary sensor carries the attribute set: repeating it on every
        # diagnostic entity would multiply the recorder's attribute churn per zone.
        if not self._spec.with_attributes:
            return {}
        data = self._zone_data or {}
        return {key: data[key] for key in _ATTR_KEYS if key in data}

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
