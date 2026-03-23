"""Config flow for the Enea Energy Meter integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

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
    CONF_BACKFILL_DAYS,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_ID,
    CONF_METER_NAME,
    CONF_TARIFF,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
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


# noinspection PyTypeChecker
class EneaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Enea Energy Meter."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,  # noqa: ARG004
    ) -> EneaOptionsFlow:
        """Return the options flow."""
        return EneaOptionsFlow()

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._meters: list[dict[str, Any]] = []
        self._connector: EneaApiClient | None = None
        self._dashboards: dict[int, dict[str, Any]] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial credentials step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_create_clientsession(self.hass)
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
                errors["base"] = ERROR_INVALID_AUTH
            except EneaApiError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception as err:
                _LOGGER.error("Unexpected error during Enea login", exc_info=err)
                errors["base"] = ERROR_UNKNOWN
            else:
                self._username = user_input[CONF_USERNAME]
                self._password = user_input[CONF_PASSWORD]
                self._meters = meters
                self._connector = connector
                self._dashboards = {
                    m["id"]: d
                    for m, d in zip(meters, dashboards)
                    if isinstance(d, dict)
                }
                return await self.async_step_select_meter()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_select_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle meter selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = int(user_input[CONF_METER_ID])
            meter = next(
                (m for m in self._meters if m["id"] == selected_id), None
            )
            if meter is None:
                errors["base"] = ERROR_UNKNOWN
            elif (
                not user_input.get(CONF_FETCH_CONSUMPTION)
                and not user_input.get(CONF_FETCH_GENERATION)
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
                        CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                    },
                    options={
                        CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                        CONF_FETCH_CONSUMPTION: user_input[CONF_FETCH_CONSUMPTION],
                        CONF_FETCH_GENERATION: user_input[CONF_FETCH_GENERATION],
                    },
                )

        options = [
            SelectOptionDict(
                value=str(m["id"]),
                label=self._meter_label(m),
            )
            for m in self._meters
        ]

        backfill_options = [
            SelectOptionDict(value="7", label="7 dni"),
            SelectOptionDict(value="30", label="30 dni"),
            SelectOptionDict(value="60", label="60 dni"),
            SelectOptionDict(value="90", label="90 dni"),
            SelectOptionDict(value="0", label="Maksymalnie (ile się da)"),
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_METER_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_BACKFILL_DAYS, default=str(DEFAULT_BACKFILL_DAYS)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=backfill_options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL_DICT
                ): DurationSelector(DurationSelectorConfig(enable_day=False)),
                vol.Required(CONF_FETCH_CONSUMPTION, default=True): BooleanSelector(),
                vol.Required(CONF_FETCH_GENERATION, default=True): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="select_meter",
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

    async def async_step_reconfigure(
        self, _user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle user-initiated reconfiguration (credential update)."""
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reconfiguration confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self._get_reconfigure_entry()
            session = async_create_clientsession(self.hass)
            connector = EneaApiClient(
                session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await connector.authenticate()
            except EneaAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except EneaApiError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception as err:
                _LOGGER.error("Unexpected error during Enea reconfigure", exc_info=err)
                errors["base"] = ERROR_UNKNOWN
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason=ABORT_RECONFIGURE_SUCCESSFUL)

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, _entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self._get_reauth_entry()
            session = async_create_clientsession(self.hass)
            connector = EneaApiClient(
                session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await connector.authenticate()
            except EneaAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except EneaApiError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception as err:
                _LOGGER.error("Unexpected error during Enea re-auth", exc_info=err)
                errors["base"] = ERROR_UNKNOWN
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason=ABORT_REAUTH_SUCCESSFUL)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )


# noinspection PyTypeChecker
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
            elif (
                not user_input.get(CONF_FETCH_CONSUMPTION)
                and not user_input.get(CONF_FETCH_GENERATION)
            ):
                errors["base"] = ERROR_AT_LEAST_ONE_FETCH_TYPE
            else:
                return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        current_interval = opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=user_input[CONF_UPDATE_INTERVAL]
                    if user_input
                    else current_interval,
                ): DurationSelector(DurationSelectorConfig(enable_day=False)),
                vol.Required(
                    CONF_FETCH_CONSUMPTION,
                    default=user_input[CONF_FETCH_CONSUMPTION]
                    if user_input
                    else opts.get(CONF_FETCH_CONSUMPTION, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FETCH_GENERATION,
                    default=user_input[CONF_FETCH_GENERATION]
                    if user_input
                    else opts.get(CONF_FETCH_GENERATION, True),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
