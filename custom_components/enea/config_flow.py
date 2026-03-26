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
    CONF_FETCH_POWER_CONSUMPTION,
    CONF_FETCH_POWER_GENERATION,
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

BACKFILL_OPTIONS = [
    SelectOptionDict(value="7", label="7 dni"),
    SelectOptionDict(value="30", label="30 dni"),
    SelectOptionDict(value="60", label="60 dni"),
    SelectOptionDict(value="90", label="90 dni"),
    SelectOptionDict(value="0", label="Maksymalnie (ile się da)"),
]

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
        """Handle options step (backfill history, update interval, fetch flags)."""
        errors: dict[str, str] = {}
        meter = self._selected_meter
        if meter is None:
            return self.async_abort(reason=ERROR_UNKNOWN)

        if user_input is not None:
            if (
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
                        CONF_FETCH_POWER_CONSUMPTION: user_input[CONF_FETCH_POWER_CONSUMPTION],
                        CONF_FETCH_POWER_GENERATION: user_input[CONF_FETCH_POWER_GENERATION],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BACKFILL_DAYS, default=str(DEFAULT_BACKFILL_DAYS)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=BACKFILL_OPTIONS,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL_DICT
                ): DurationSelector(DurationSelectorConfig(enable_day=False)),
                vol.Required(CONF_FETCH_CONSUMPTION, default=True): BooleanSelector(),
                vol.Required(CONF_FETCH_GENERATION, default=True): BooleanSelector(),
                vol.Required(CONF_FETCH_POWER_CONSUMPTION, default=False): BooleanSelector(),
                vol.Required(CONF_FETCH_POWER_GENERATION, default=False): BooleanSelector(),
            }
        )

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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle user-initiated reconfiguration (credential update)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_validate_and_update_credentials(
                self._get_reconfigure_entry(),
                user_input,
                "Unexpected error during Enea reconfigure",
            ) or {}
            if not errors:
                return self.async_abort(reason=ABORT_RECONFIGURE_SUCCESSFUL)

        return self.async_show_form(
            step_id="reconfigure",
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
            errors = await self._async_validate_and_update_credentials(
                self._get_reauth_entry(),
                user_input,
                "Unexpected error during Enea re-auth",
            ) or {}
            if not errors:
                return self.async_abort(reason=ABORT_REAUTH_SUCCESSFUL)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
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
            elif (
                not user_input.get(CONF_FETCH_CONSUMPTION)
                and not user_input.get(CONF_FETCH_GENERATION)
            ):
                errors["base"] = ERROR_AT_LEAST_ONE_FETCH_TYPE
            else:
                return self.async_create_entry(data=user_input)

        opts = self.config_entry.options

        def _val(key: str, fallback: object) -> object:
            """Return submitted value on re-render after error, otherwise current option."""
            return user_input[key] if user_input else opts.get(key, fallback)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=_val(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT),
                ): DurationSelector(DurationSelectorConfig(enable_day=False)),
                vol.Required(
                    CONF_FETCH_CONSUMPTION,
                    default=_val(CONF_FETCH_CONSUMPTION, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FETCH_GENERATION,
                    default=_val(CONF_FETCH_GENERATION, True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FETCH_POWER_CONSUMPTION,
                    default=_val(CONF_FETCH_POWER_CONSUMPTION, False),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FETCH_POWER_GENERATION,
                    default=_val(CONF_FETCH_POWER_GENERATION, False),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
