"""Date input entities for Enea bill estimation.

Two editable DateEntity instances per meter allow the user to set the dates of
the previous and last meter readings.  Changing either date immediately
recomputes the bill estimates (via coordinator.async_recompute_bills) and
updates the corresponding EneaBillSensor values.

Entities are only created when the enea_prices integration is configured with
a tariff matching this meter, because the bill computation requires tariff data.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.components.date import DateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EneaConfigEntry
from .connector import get_active_meter
from .const import (
    BILL_KEY_LAST_READING,
    BILL_KEY_PREV_READING,
    CONF_METER_NAME,
    DEFAULT_NAME,
    DOMAIN,
    PORTAL_URL,
)
from .coordinator import EneaUpdateCoordinator
from .costs import find_tariff_group


def _device_info(meter_code: str, data: dict[str, Any] | None) -> DeviceInfo:
    """Build DeviceInfo for the given meter."""
    active = get_active_meter(data) if data else None
    return DeviceInfo(
        identifiers={(DOMAIN, meter_code)},
        name=f"{DEFAULT_NAME} {meter_code}",
        manufacturer="Enea",
        model=active["typeName"] if active else None,
        serial_number=active["serialNumber"] if active else None,
        configuration_url=PORTAL_URL,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EneaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enea bill date entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    meter_code = entry.data[CONF_METER_NAME]
    tariff_name = (coordinator.data or {}).get("tariffGroupName")

    if find_tariff_group(hass, tariff_name) is None:
        return

    async_add_entities([
        EneaBillDateEntity(coordinator, meter_code, BILL_KEY_PREV_READING),
        EneaBillDateEntity(coordinator, meter_code, BILL_KEY_LAST_READING),
    ])


class EneaBillDateEntity(RestoreEntity, DateEntity):
    """Editable date entity marking a billing period boundary.

    One entity represents the previous reading date (d1, exclusive period
    start), the other represents the last reading date (d2, inclusive period
    end for the closed bill and exclusive start for the running bill).

    The date is persisted via RestoreEntity so it survives HA restarts.
    On restore, the coordinator fields are updated and a bill recompute is
    scheduled so the sensors show correct values immediately after startup.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        key: str,
    ) -> None:
        """Initialize the date entity."""
        self._coordinator = coordinator
        self._meter_code = meter_code
        self._key = key
        self._attr_unique_id = f"enea-{meter_code}-{key}"
        self._attr_translation_key = key
        self._attr_device_info = _device_info(meter_code, coordinator.data)
        self._attr_native_value: date | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known date value and inject it into the coordinator."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                restored = date.fromisoformat(last_state.state)
                self._attr_native_value = restored
                self._write_to_coordinator(restored)
            except (ValueError, TypeError):
                pass
        # Schedule a recompute after both entities may have restored their dates.
        self.hass.async_create_task(self._coordinator.async_recompute_bills())

    def _write_to_coordinator(self, value: date | None) -> None:
        """Write the date into the corresponding coordinator field."""
        if self._key == BILL_KEY_PREV_READING:
            self._coordinator.bill_prev_reading = value
        else:
            self._coordinator.bill_last_reading = value

    @property
    def native_value(self) -> date | None:
        """Return the current date value."""
        return self._attr_native_value

    async def async_set_value(self, value: date) -> None:
        """Handle user setting a new date; recompute bill estimates."""
        self._attr_native_value = value
        self._write_to_coordinator(value)
        self.async_write_ha_state()
        await self._coordinator.async_recompute_bills()
