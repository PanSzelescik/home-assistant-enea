"""Fix flows for the installation issues: write the meter's value into enea_prices.

An installation issue (see issues.py) says that an enea_prices setting disagrees
with the meter.  Confirming its fix writes the meter's value into the matching
enea_prices entry and reloads it; enea_prices reloads the Enea entries of its
tariff group in turn, whose next statistics step finds nothing left to raise.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import ENEA_PRICES_CONF_TARIFF, ENEA_PRICES_DOMAIN


def _prices_entry(hass: HomeAssistant, tariff_name: str) -> ConfigEntry | None:
    """Return the enea_prices entry of a tariff group, matched like find_prices_config."""
    wanted = tariff_name.casefold()
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN)
            if (entry.data.get(ENEA_PRICES_CONF_TARIFF) or "").casefold() == wanted
        ),
        None,
    )


class InstallationRepairFlow(ConfirmRepairFlow):
    """Confirm, then set one enea_prices setting to what the meter shows."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Keep the issue data: the tariff group, the setting and its new value."""
        super().__init__()
        self._data = data

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show what changes; once confirmed, change it and reload enea_prices."""
        if user_input is not None:
            entry = _prices_entry(self.hass, self._data["tariff"])
            if entry is None:
                return self.async_abort(reason="prices_entry_missing")
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, self._data["setting"]: self._data["value"]}
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the fix flow of an installation issue."""
    return InstallationRepairFlow(data or {})
