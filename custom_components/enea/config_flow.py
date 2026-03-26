"""Config flow for the Enea Energy Meter integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DurationSelector,
    DurationSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .connector import EneaApiClient, EneaAuthError, EneaApiError, format_address
from .const import (
    ABORT_REAUTH_SUCCESSFUL,
    ABORT_RECONFIGURE_SUCCESSFUL,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_FETCH_POWER_CONSUMPTION,
    CONF_FETCH_POWER_GENERATION,
    CONF_METER_ID,
    CONF_METER_NAME,
    CONF_TARIFF,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_DICT,
    MIN_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    ERROR_AT_LEAST_ONE_FETCH_TYPE,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_AUTH,
    ERROR_INTERVAL_TOO_SHORT,
    ERROR_UNKNOWN,
)

_LOGGER = logging.getLogger(__name__)

def _options_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Build the options schema for update interval and fetch flags.

    Shared by the initial configure step and the options flow so both
    forms stay in sync when options are added or removed.
    """
    return vol.Schema({
        vol.Required(
            CONF_UPDATE_INTERVAL,
            default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT),
        ): DurationSelector(DurationSelectorConfig(enable_day=False)),
        vol.Required(
            CONF_FETCH_CONSUMPTION,
            default=defaults.get(CONF_FETCH_CONSUMPTION, True),
        ): BooleanSelector(),
        vol.Required(
            CONF_FETCH_GENERATION,
            default=defaults.get(CONF_FETCH_GENERATION, True),
        ): BooleanSelector(),
        vol.Required(
            CONF_FETCH_POWER_CONSUMPTION,
            default=defaults.get(CONF_FETCH_POWER_CONSUMPTION, False),
        ): BooleanSelector(),
        vol.Required(
            CONF_FETCH_POWER_GENERATION,
            default=defaults.get(CONF_FETCH_POWER_GENERATION, False),
        ): BooleanSelector(),
    })


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class EneaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Enea Energy Meter."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EneaOptionsFlow:
        """Return the options flow."""
        return EneaOptionsFlow()

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._meters: list[dict[str, Any]] = []
        self._dashboards: dict[int, dict[str, Any]] = {}
        self._selected_meter: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial credentials step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_login(user_input)
            if not errors:
                if len(self._meters) == 1:
                    self._selected_meter = self._meters[0]
                    return await self.async_step_configure()
                return await self.async_step_select_meter()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def _async_login(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Authenticate with the API and load the meter list with dashboards.

        On success updates self._username/_password/_meters/_dashboards and
        returns an empty dict.  On failure returns an errors dict for the form.
        """
        session = async_create_clientsession(self.hass)
        try:
            connector = EneaApiClient(
                session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await connector.authenticate()
                meters = await connector.get_meters()
                dashboards = await asyncio.gather(
                    *[connector.get_ppe_dashboard(m["id"]) for m in meters],
                    return_exceptions=True,
                )
            except EneaAuthError:
                return {"base": ERROR_INVALID_AUTH}
            except EneaApiError:
                return {"base": ERROR_CANNOT_CONNECT}
            except Exception as err:
                _LOGGER.error("Unexpected error during Enea login", exc_info=err)
                return {"base": ERROR_UNKNOWN}
        finally:
            await session.close()

        self._username = user_input[CONF_USERNAME]
        self._password = user_input[CONF_PASSWORD]
        self._meters = meters
        self._dashboards = {
            m["id"]: d
            for m, d in zip(meters, dashboards)
            if isinstance(d, dict)
        }
        return {}

    async def async_step_select_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle meter selection step (shown only when account has multiple meters)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = int(user_input[CONF_METER_ID])
            meter = next(
                (m for m in self._meters if m["id"] == selected_id), None
            )
            if meter is None:
                errors["base"] = ERROR_UNKNOWN
            else:
                self._selected_meter = meter
                return await self.async_step_configure()

        options = [
            SelectOptionDict(
                value=str(m["id"]),
                label=self._meter_label(m),
            )
            for m in self._meters
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_METER_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_meter",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle options step (update interval, fetch flags)."""
        errors: dict[str, str] = {}
        meter = self._selected_meter
        if meter is None:
            return self.async_abort(reason=ERROR_UNKNOWN)

        if user_input is not None:
            duration = user_input[CONF_UPDATE_INTERVAL]
            total_minutes = (
                int(duration.get("hours", 0)) * 60
                + int(duration.get("minutes", 0))
            )
            if total_minutes < MIN_UPDATE_INTERVAL_MINUTES:
                errors[CONF_UPDATE_INTERVAL] = ERROR_INTERVAL_TOO_SHORT
            elif not (
                user_input.get(CONF_FETCH_CONSUMPTION)
                or user_input.get(CONF_FETCH_GENERATION)
                or user_input.get(CONF_FETCH_POWER_CONSUMPTION)
                or user_input.get(CONF_FETCH_POWER_GENERATION)
            ):
                errors["base"] = ERROR_AT_LEAST_ONE_FETCH_TYPE
            else:
                await self.async_set_unique_id(meter["code"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Enea {meter['code']}",
                    data={
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_METER_ID: meter["id"],
                        CONF_METER_NAME: meter["code"],
                        CONF_TARIFF: meter.get("tariffGroup", {}).get("name", ""),
                    },
                    options={
                        CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                        CONF_FETCH_CONSUMPTION: user_input[CONF_FETCH_CONSUMPTION],
                        CONF_FETCH_GENERATION: user_input[CONF_FETCH_GENERATION],
                        CONF_FETCH_POWER_CONSUMPTION: user_input[CONF_FETCH_POWER_CONSUMPTION],
                        CONF_FETCH_POWER_GENERATION: user_input[CONF_FETCH_POWER_GENERATION],
                    },
                )

        schema = _options_schema(user_input if user_input else {})

        return self.async_show_form(
            step_id="configure",
            data_schema=schema,
            errors=errors,
        )

    def _meter_label(self, meter: dict[str, Any]) -> str:
        """Build the dropdown label for a meter."""
        tariff = meter.get("tariffGroup", {}).get("name", "?")
        dashboard = self._dashboards.get(meter["id"], {})
        address = format_address(dashboard.get("address"))
        if address:
            return f"{meter['code']} ({tariff}) – {address}"
        return f"{meter['code']} ({tariff})"

    async def _async_validate_and_update_credentials(
        self,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any],
        error_log_msg: str,
    ) -> dict[str, str] | None:
        """Validate credentials against the API and update the config entry on success.

        Returns a dict of errors if validation fails, or None on success.
        On success, the entry is updated in place and reloaded.
        """
        session = async_create_clientsession(self.hass)
        try:
            connector = EneaApiClient(
                session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await connector.authenticate()
            except EneaAuthError:
                return {"base": ERROR_INVALID_AUTH}
            except EneaApiError:
                return {"base": ERROR_CANNOT_CONNECT}
            except Exception as err:
                _LOGGER.error(error_log_msg, exc_info=err)
                return {"base": ERROR_UNKNOWN}
        finally:
            await session.close()

        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            },
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        return None

    async def _async_credentials_step(
        self,
        user_input: dict[str, Any] | None,
        step_id: str,
        entry: config_entries.ConfigEntry,
        abort_reason: str,
    ) -> config_entries.ConfigFlowResult:
        """Shared logic for credential-update steps (reconfigure and reauth).

        Shows STEP_USER_SCHEMA, validates on submit, aborts on success.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_validate_and_update_credentials(
                entry, user_input, f"Unexpected error during Enea {step_id}"
            ) or {}
            if not errors:
                return self.async_abort(reason=abort_reason)
        return self.async_show_form(
            step_id=step_id, data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle user-initiated reconfiguration (credential update)."""
        return await self._async_credentials_step(
            user_input, "reconfigure", self._get_reconfigure_entry(), ABORT_RECONFIGURE_SUCCESSFUL
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication triggered by an auth failure.

        HA triggers this initially with the full config entry data (which contains
        CONF_METER_ID and CONF_USERNAME). On form submission it is called again
        with only the credential fields (CONF_USERNAME + CONF_PASSWORD, no
        CONF_METER_ID).  We treat a payload as a form submission only when it
        carries both credential keys but not CONF_METER_ID, so that an
        unexpected or minimal initial payload never reaches credential validation.
        """
        credentials: dict[str, Any] | None = (
            dict(entry_data)
            if (
                CONF_USERNAME in entry_data
                and CONF_PASSWORD in entry_data
                and CONF_METER_ID not in entry_data
            )
            else None
        )
        return await self._async_credentials_step(
            credentials, "reauth", self._get_reauth_entry(), ABORT_REAUTH_SUCCESSFUL
        )


class EneaOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Enea Energy Meter."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            duration = user_input[CONF_UPDATE_INTERVAL]
            total_minutes = (
                int(duration.get("hours", 0)) * 60
                + int(duration.get("minutes", 0))
            )
            if total_minutes < MIN_UPDATE_INTERVAL_MINUTES:
                errors[CONF_UPDATE_INTERVAL] = ERROR_INTERVAL_TOO_SHORT
            elif not (
                user_input.get(CONF_FETCH_CONSUMPTION)
                or user_input.get(CONF_FETCH_GENERATION)
                or user_input.get(CONF_FETCH_POWER_CONSUMPTION)
                or user_input.get(CONF_FETCH_POWER_GENERATION)
            ):
                errors["base"] = ERROR_AT_LEAST_ONE_FETCH_TYPE
            else:
                return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = _options_schema(user_input if user_input else opts)

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
