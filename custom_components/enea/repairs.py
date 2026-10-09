"""Fix flows of the Enea repair issues that enea_prices settles.

- An installation issue (see issues.py) says that an enea_prices setting
  disagrees with the meter.  Confirming its fix writes the meter's value into
  the matching enea_prices entry and reloads it; enea_prices reloads the Enea
  entries of its tariff group in turn, whose next statistics step finds
  nothing left to raise.
- The prices setup issue says that enea_prices is installed but does not price
  the meter's tariff group yet.  Confirming it opens the enea_prices config
  flow, which the meter pre-fills.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, FlowType, RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant

from .billing import find_prices_entry
from .const import ENEA_PRICES_DOMAIN, ISSUE_PRICES_NOT_CONFIGURED


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
            entry = find_prices_entry(self.hass, self._data["tariff"])
            if entry is None:
                return self.async_abort(reason="prices_entry_missing")
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, self._data["setting"]: self._data["value"]}
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
        return await super().async_step_confirm(user_input)


class PricesSetupRepairFlow(ConfirmRepairFlow):
    """Confirm, then continue in the enea_prices config flow.

    The issue is gone once this flow finishes.  Leaving the config flow without
    adding the entry raises it again at the next statistics step.
    """

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show what enea_prices adds; once confirmed, start adding it."""
        if user_input is None:
            return await super().async_step_confirm(user_input)
        result = await self.hass.config_entries.flow.async_init(
            ENEA_PRICES_DOMAIN, context={"source": SOURCE_USER}
        )
        return self.async_create_entry(
            data={}, next_flow=(FlowType.CONFIG_FLOW, result["flow_id"])
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the fix flow of an issue, by the issue key its id starts with."""
    if issue_id.startswith(ISSUE_PRICES_NOT_CONFIGURED):
        return PricesSetupRepairFlow()
    return InstallationRepairFlow(data or {})
