"""Diagnostics support for the Enea Energy Meter integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import EneaConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_ADDRESS}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: EneaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Triggers a fresh data fetch so the diagnostics always reflect the current
    state from the Portal Odbiorcy Enea (not a potentially stale cached value).
    """
    coordinator = entry.runtime_data.coordinator

    await coordinator.async_refresh()

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
        },
        "meter_data": async_redact_data(coordinator.data, TO_REDACT),
    }
