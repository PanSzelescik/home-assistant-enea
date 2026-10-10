"""The stand-in enea_prices' config flow: one form with the four settings."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult


class EneaPricesConfigFlow(ConfigFlow, domain="enea_prices"):
    """Ask for the tariff group and the installation, then add the entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the form, or add the entry for the submitted group."""
        if user_input is not None:
            await self.async_set_unique_id(user_input["tariff"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Ceny {user_input['tariff']}", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("tariff"): str,
                vol.Required("phases"): int,
                vol.Required("annual_kwh"): int,
                vol.Required("billing_months"): int,
            }),
        )
