"""Sensor platform for the Enea Energy Meter integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EneaConfigEntry
from .connector import format_address, get_active_meter
from .const import (
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_NAME,
    CONST_PORTAL_URL,
    COST_ZONE_DISPLAY,
    DEFAULT_NAME,
    DOMAIN,
    MEASUREMENT_ID_CONSUMPTION,
    SENSOR_KEY_ADDRESS,
    SENSOR_KEY_CAPACITY,
    SENSOR_KEY_METER_MODEL,
    SENSOR_KEY_READING_DATE,
    SENSOR_KEY_STATUS,
    SENSOR_KEY_TARIFF,
    UNIT_COST,
)
from .coordinator import EneaUpdateCoordinator
from .costs import find_tariff_group, get_cost_unique_id


def _get_device_info(meter_code: str, data: dict[str, Any] | None) -> DeviceInfo:
    """Build DeviceInfo, enriched with physical meter details when data is available."""
    active = get_active_meter(data) if data else None
    return DeviceInfo(
        identifiers={(DOMAIN, meter_code)},
        name=f"{DEFAULT_NAME} {meter_code}",
        manufacturer="Enea",
        model=active["typeName"] if active else None,
        serial_number=active["serialNumber"] if active else None,
        configuration_url=CONST_PORTAL_URL,
    )


# ---------------------------------------------------------------------------
# Static diagnostic sensors (always created, data from dashboard endpoint)
# ---------------------------------------------------------------------------


def _address_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return address fields as a flat dict, omitting null values."""
    addr = data.get("address")
    if not addr:
        return {}
    return {
        k: v
        for k, v in {
            "street": addr.get("street"),
            "house_number": addr.get("houseNum"),
            "apartment_number": addr.get("apartmentNum"),
            "post_code": addr.get("postCode"),
            "city": addr.get("city"),
            "district": addr.get("district"),
            "parcel_number": addr.get("parcelNum"),
        }.items()
        if v is not None
    }


def _meter_model_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return assembly/disassembly timestamps of the active meter as ISO strings."""
    m = get_active_meter(data)
    if not m:
        return {}
    return {
        k: datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
        for k, ts in {
            "assembly_date": m.get("assemblyDate"),
            "disassembly_date": m.get("disassemblyDate"),
        }.items()
        if ts is not None
    }


def _get_reading_date(data: dict[str, Any]) -> datetime | None:
    """Return the last reading timestamp from dashboard data, or None if unavailable."""
    ts = next(
        (
            cv["readingDate"]
            for cv in data.get("currentValues", [])
            if cv.get("measurementId") == MEASUREMENT_ID_CONSUMPTION and cv.get("readingDate")
        ),
        None,
    )
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None


@dataclass(frozen=True, kw_only=True)
class EneaSensorEntityDescription(SensorEntityDescription):
    """Extended sensor description for Enea diagnostic sensors."""

    value_fn: Callable[[dict[str, Any]], Any] | None = None
    attr_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


SENSOR_DESCRIPTIONS: tuple[EneaSensorEntityDescription, ...] = (
    EneaSensorEntityDescription(
        key=SENSOR_KEY_TARIFF,
        translation_key=SENSOR_KEY_TARIFF,
        icon="mdi:tag-text",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("tariffGroupName"),
        attr_fn=lambda data: {
            "zones": next(
                (
                    cv["ppeZones"]
                    for cv in data.get("currentValues", [])
                    if cv.get("measurementId") == MEASUREMENT_ID_CONSUMPTION
                ),
                [],
            ),
        },
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_CAPACITY,
        translation_key=SENSOR_KEY_CAPACITY,
        icon="mdi:flash-triangle",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("agreementPower"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_STATUS,
        translation_key=SENSOR_KEY_STATUS,
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("detailedStatus"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_ADDRESS,
        translation_key=SENSOR_KEY_ADDRESS,
        icon="mdi:map-marker",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: format_address(data.get("address")),
        attr_fn=_address_attrs,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_READING_DATE,
        translation_key=SENSOR_KEY_READING_DATE,
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_reading_date,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_METER_MODEL,
        translation_key=SENSOR_KEY_METER_MODEL,
        icon="mdi:meter-electric-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (get_active_meter(data) or {}).get("typeName"),
        attr_fn=_meter_model_attrs,
    ),
)


# ---------------------------------------------------------------------------
# Energy sensors (static total + dynamic per-zone)
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EneaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enea sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    meter_code = entry.data[CONF_METER_NAME]
    data = coordinator.data or {}
    fetch_consumption = entry.options.get(CONF_FETCH_CONSUMPTION, True)
    fetch_generation = entry.options.get(CONF_FETCH_GENERATION, True)

    sensors: list[SensorEntity] = []

    # Diagnostic sensors
    sensors.extend(
        EneaSensor(coordinator, meter_code, description)
        for description in SENSOR_DESCRIPTIONS
    )

    # Energy sensors — total (always) + per-zone (dynamic)
    for cv in data.get("currentValues", []):
        measurement_id: int = cv["measurementId"]
        is_consumption = measurement_id == MEASUREMENT_ID_CONSUMPTION
        if is_consumption and not fetch_consumption:
            continue
        if not is_consumption and not fetch_generation:
            continue
        prefix = "consumption" if is_consumption else "generation"
        type_label = "pobrana" if is_consumption else "oddana"

        # Total (sum of all zones)
        sensors.append(
            EneaEnergySensor(
                coordinator=coordinator,
                meter_code=meter_code,
                measurement_id=measurement_id,
                zone_key="valueNoZones",
                unique_key=f"{prefix}_total",
                sensor_name=None,  # uses translation_key
                translation_key=f"{prefix}_total",
            )
        )

        # Per-zone — name includes type to distinguish consumption vs generation
        for i, zone_label in enumerate(cv.get("ppeZones", []), start=1):
            zone_key = f"valueZone{i}"
            if cv.get(zone_key) is not None:
                short_name = zone_label.split(" ")[0]  # "Dzień 1.8.1" → "Dzień"
                sensors.append(
                    EneaEnergySensor(
                        coordinator=coordinator,
                        meter_code=meter_code,
                        measurement_id=measurement_id,
                        zone_key=zone_key,
                        unique_key=f"{prefix}_zone{i}",
                        sensor_name=f"Energia {type_label} – {short_name}",
                        translation_key=None,
                    )
                )

    # Cost sensors — created only when enea_prices is configured with matching tariff
    tariff_name = data.get("tariffGroupName")
    tariff = find_tariff_group(hass, tariff_name)
    if tariff is not None:
        period = tariff.get_current_period()
        if period is not None:
            for direction, is_consumption in (("pobrana", True), ("oddana", False)):
                if is_consumption and not fetch_consumption:
                    continue
                if not is_consumption and not fetch_generation:
                    continue
                for zone in period.zones:
                    zone_str = str(zone)
                    sensors.append(
                        EneaCostSensor(
                            coordinator=coordinator,
                            meter_code=meter_code,
                            direction=direction,
                            zone_str=zone_str,
                            zone_display=COST_ZONE_DISPLAY.get(zone_str, zone_str),
                        )
                    )

    async_add_entities(sensors)


class EneaSensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):
    """Diagnostic sensor entity for an Enea meter."""

    entity_description: EneaSensorEntityDescription  # pyright: ignore[reportIncompatibleVariableOverride]
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        description: EneaSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description  # pyright: ignore[reportIncompatibleVariableOverride]
        self._meter_code = meter_code
        self._attr_unique_id = f"enea-{meter_code}-{description.key}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return whether the entity is available."""
        return super().available

    @cached_property
    def native_value(self) -> Any:
        """Return the sensor state value."""
        if self.coordinator.data is None or self.entity_description.value_fn is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if self.entity_description.attr_fn is None:
            return None
        if self.coordinator.data is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)


class EneaEnergySensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):
    """Energy sensor for a specific measurement/zone of an Enea meter."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        measurement_id: int,
        zone_key: str,
        unique_key: str,
        sensor_name: str | None,
        translation_key: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._meter_code = meter_code
        self._measurement_id = measurement_id
        self._zone_key = zone_key
        self._attr_unique_id = f"enea-{meter_code}-{unique_key}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)

        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = sensor_name

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return whether the entity is available."""
        return super().available

    @cached_property
    def native_value(self) -> float | None:
        """Return the energy value in kWh."""
        data = self.coordinator.data
        if not data:
            return None
        for cv in data.get("currentValues", []):
            if cv.get("measurementId") == self._measurement_id:
                zone_data = cv.get(self._zone_key)
                if zone_data is None:
                    return None
                return zone_data.get("value")
        return None


class EneaCostSensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):
    """Sensor tracking accumulated electricity cost (PLN) for a single tariff zone.

    Created only when the enea_prices integration is configured with a tariff
    matching the meter's tariffGroupName.  One sensor is created per active
    zone (e.g. Dzień / Noc for G12) per energy direction (consumed/returned).

    Historical cost statistics are injected by costs.py using
    async_import_statistics so that the Energy Dashboard can use this entity
    as "entity tracking total costs".
    """

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT_COST
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        direction: str,
        zone_str: str,
        zone_display: str,
    ) -> None:
        """Initialize a cost sensor for the given meter, direction and zone."""
        super().__init__(coordinator)
        self._meter_code = meter_code
        self._attr_unique_id = get_cost_unique_id(meter_code, direction, zone_str)
        self._attr_name = f"Koszt energii {direction} \u2013 {zone_display}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return whether the entity is available."""
        return super().available

    @property
    def native_value(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the last known cumulative cost sum, or None before first injection."""
        unique_id = self._attr_unique_id or ""
        return self.coordinator.cost_sums.get(unique_id)

