"""Binary sensor platform for the Enea Energy Meter integration.

Diagnostic on/off states that the Portal Odbiorcy Enea shows as icons next to
the meter: whether the meter communicates with Enea and whether it supports
the HAN port at all.  Data comes from the dashboard endpoint.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EneaConfigEntry
from .const import (
    BINARY_SENSOR_KEY_HAN_AVAILABLE,
    BINARY_SENSOR_KEY_TRANSMISSION,
    CONF_METER_NAME,
)
from .coordinator import EneaUpdateCoordinator
from .sensor import _get_device_info


def _optional_bool(value: Any) -> bool | None:
    """Return the API flag as a bool, keeping None when the field is absent."""
    return None if value is None else bool(value)


@dataclass(frozen=True, kw_only=True)
class EneaBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Extended binary sensor description for Enea diagnostic states."""

    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[EneaBinarySensorEntityDescription, ...] = (
    EneaBinarySensorEntityDescription(
        key=BINARY_SENSOR_KEY_TRANSMISSION,
        translation_key=BINARY_SENSOR_KEY_TRANSMISSION,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _optional_bool(data.get("transmissionStatus")),
    ),
    EneaBinarySensorEntityDescription(
        key=BINARY_SENSOR_KEY_HAN_AVAILABLE,
        translation_key=BINARY_SENSOR_KEY_HAN_AVAILABLE,
        icon="mdi:access-point-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _optional_bool(data.get("hanAvailable")),
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: EneaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enea binary sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    meter_code = entry.data[CONF_METER_NAME]
    async_add_entities(
        EneaBinarySensor(coordinator, meter_code, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class EneaBinarySensor(CoordinatorEntity[EneaUpdateCoordinator], BinarySensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Diagnostic binary sensor entity for an Enea meter."""

    entity_description: EneaBinarySensorEntityDescription  # pyright: ignore[reportIncompatibleVariableOverride]
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        description: EneaBinarySensorEntityDescription,
    ) -> None:
        """Initialize a diagnostic binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description  # pyright: ignore[reportIncompatibleVariableOverride]
        self._attr_unique_id = f"enea-{meter_code}-{description.key}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Compute the state, None when the Portal Odbiorcy Enea omits the field."""
        data = self.coordinator.data
        self._attr_is_on = self.entity_description.value_fn(data) if data is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute the state from the fresh coordinator data, then write it.

        Home Assistant only invalidates its cached entity properties when an
        _attr_ field is assigned, so the state must be assigned here rather
        than computed in a (cached) property — that would keep the first value
        until a restart.
        """
        self._update_attrs()
        super()._handle_coordinator_update()
