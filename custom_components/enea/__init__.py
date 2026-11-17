"""Enea Energy Meter integration for Home Assistant."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

# "date" itself would shadow the date platform module of this package.
from datetime import date as date_type
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_CLOSE,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient
from .const import (
    ATTR_DAYS_BACK,
    ATTR_END_DATE,
    ATTR_START_DATE,
    BACKFILL_DEFAULT_DAYS,
    BACKFILL_MAX_DAYS_BACK,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_FETCH_POWER_CONSUMPTION,
    CONF_FETCH_POWER_GENERATION,
    CONF_METER_ID,
    CONF_METER_NAME,
    CONF_NET_METERING,
    CONF_NOTIFICATIONS,
    CONF_UPDATE_INTERVAL,
    COST_PRICES_STORAGE_KEY,
    COST_PRICES_STORAGE_VERSION,
    DEFAULT_NET_METERING,
    DEFAULT_UPDATE_INTERVAL_DICT,
    DOMAIN,
    ERROR_END_BEFORE_START,
    NET_METERING_RATIOS,
    NOTIFICATIONS_STORAGE_VERSION,
    PLATFORMS,
    SERVICE_BACKFILL,
    SERVICE_REFRESH,
)
from .coordinator import EneaUpdateCoordinator
from .issues import async_delete_issues
from .notifications import EneaNotifications, storage_key

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_DEVICE_IDS = vol.All(cv.ensure_list, [cv.string])

SERVICE_REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_DEVICE_ID): _DEVICE_IDS})

SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): _DEVICE_IDS,
        vol.Optional(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_DAYS_BACK): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=BACKFILL_MAX_DAYS_BACK)
        ),
    }
)

@dataclass
class EneaAccount:
    """What the meters of one Portal Odbiorcy Enea account share."""

    client: EneaApiClient
    notifications: EneaNotifications
    unsub_close: Callable[[], None]
    """Drops the listener that closes the session when Home Assistant stops."""
    entry_ids: set[str] = field(default_factory=set)
    """The config entries using the account, those whose setup failed included:
    they keep the account for their next attempt."""

    async def async_close(self) -> None:
        """Stop watching the notifications and close the session; no meter is left."""
        self.notifications.unsubscribe()
        self.unsub_close()
        await self.client.async_close()


@dataclass
class EneaRuntimeData:
    """Runtime data stored in the config entry."""

    coordinator: EneaUpdateCoordinator


type EneaConfigEntry = ConfigEntry[EneaRuntimeData]


def _matching_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> Iterator[EneaUpdateCoordinator]:
    """Yield coordinators for entries matching the device_id filter in a service call.

    When no device_id is provided, yields coordinators for all loaded entries.
    The devices are looked up by their config entry: device_registry's
    async_get_device by identifier is deprecated (identifiers are no longer
    unique across config entries) and stops working in Home Assistant 2027.8.
    """
    device_ids: list[str] = call.data.get(ATTR_DEVICE_ID, [])
    dev_reg = dr.async_get(hass)
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        if config_entry.state is not ConfigEntryState.LOADED:
            continue
        devices = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
        if devices and (not device_ids or any(device.id in device_ids for device in devices)):
            yield config_entry.runtime_data.coordinator


async def _async_handle_refresh(call: ServiceCall) -> None:
    """Handle the "enea.refresh" action.

    Triggers an immediate data refresh for each matching device.
    If no device_id is provided, all Enea devices are refreshed.
    """
    for coordinator in _matching_coordinators(call.hass, call):
        await coordinator.async_request_refresh()


async def _async_handle_backfill(call: ServiceCall) -> None:
    """Handle the "enea.backfill" action.

    Imports historical statistics for the specified date range.
    Accepts start_date/end_date, days_back, or defaults to the last
    BACKFILL_DEFAULT_DAYS days when no parameters are given.
    """
    yesterday = dt_util.now().date() - timedelta(days=1)

    start_date: date_type | None = call.data.get(ATTR_START_DATE)
    end_date: date_type = yesterday
    if start_date is not None:
        end_date = call.data.get(ATTR_END_DATE, yesterday)
        if end_date < start_date:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=ERROR_END_BEFORE_START,
                translation_placeholders={
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
    else:
        days_back: int = call.data.get(ATTR_DAYS_BACK, BACKFILL_DEFAULT_DAYS)
        start_date = yesterday - timedelta(days=days_back - 1)

    for coordinator in _matching_coordinators(call.hass, call):
        await coordinator.async_backfill(start_date, end_date)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions; they stay for as long as the integration is loaded.

    An action registered with the first config entry and removed with the last
    is missing whenever no entry is loaded, and an automation calling it then
    fails with "action not found" instead of doing nothing.
    """
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_handle_refresh, schema=SERVICE_REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BACKFILL, _async_handle_backfill, schema=SERVICE_BACKFILL_SCHEMA
    )
    return True


def _async_create_account(hass: HomeAssistant, username: str, password: str) -> EneaAccount:
    """Return a new account: its own session, client and messages.

    The session is closed by the account, not by Home Assistant: one created
    with auto_cleanup is closed with the config entry whose setup created it,
    which would leave the account's other meters with a dead client the first
    time that entry is reloaded or its setup fails.
    """
    session = async_create_clientsession(hass, auto_cleanup=False)
    client = EneaApiClient(session, username, password)

    @callback
    def _close_on_stop(_event: Event) -> None:
        """Close the session when Home Assistant stops; entries are not unloaded then."""
        client.detach()

    unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _close_on_stop)
    return EneaAccount(client, EneaNotifications(hass, client, username), unsub)


async def _async_release_account(
    hass: HomeAssistant, entry: EneaConfigEntry, keep: str | None = None
) -> None:
    """Take the entry out of its account and close an account left with no entries.

    The account is found by the entry, not by the entry's login: by the time an
    entry given another login is unloaded, its data already names the new one.
    keep names the account the entry is about to join, which is left alone.
    """
    accounts: dict[str, EneaAccount] = hass.data.get(DOMAIN, {})
    for username, account in list(accounts.items()):
        if username == keep or entry.entry_id not in account.entry_ids:
            continue
        account.entry_ids.discard(entry.entry_id)
        if not account.entry_ids:
            del accounts[username]
            await account.async_close()


async def async_setup_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Set up Enea from a config entry."""
    accounts: dict[str, EneaAccount] = hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Reuse existing client for accounts with multiple meters (shared session/cookie).
    # If credentials changed (e.g. after reauth), update them in the shared client.
    # The messages are checked per account too: one request for all its meters.
    # An entry whose setup failed under another login never got unloaded.
    await _async_release_account(hass, entry, keep=username)
    account = accounts.get(username)
    if account is not None:
        account.client.update_credentials(password)
    else:
        account = accounts[username] = _async_create_account(hass, username, password)
    account.entry_ids.add(entry.entry_id)

    duration = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT)
    update_interval = timedelta(
        hours=int(duration.get("hours", 0)),
        minutes=int(duration.get("minutes", 0)),
        seconds=int(duration.get("seconds", 0)),
    )
    update_coordinator = EneaUpdateCoordinator(
        hass,
        entry,
        account.client,
        entry.data[CONF_METER_ID],
        meter_code=entry.data[CONF_METER_NAME],
        update_interval=update_interval,
        fetch_consumption=entry.options.get(CONF_FETCH_CONSUMPTION, True),
        fetch_generation=entry.options.get(CONF_FETCH_GENERATION, True),
        fetch_power_consumption=entry.options.get(CONF_FETCH_POWER_CONSUMPTION, False),
        fetch_power_generation=entry.options.get(CONF_FETCH_POWER_GENERATION, False),
        net_metering_ratio=NET_METERING_RATIOS.get(
            entry.options.get(CONF_NET_METERING, DEFAULT_NET_METERING)
        ),
        notifications=(
            account.notifications if entry.options.get(CONF_NOTIFICATIONS, True) else None
        ),
    )
    entry.runtime_data = EneaRuntimeData(coordinator=update_coordinator)
    await update_coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Unload a config entry; the last one of an account closes its session."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await _async_release_account(hass, entry)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> None:
    """Delete the repair issues and the stores of a removed meter entry.

    The messages seen are the account's: they go with its last meter.
    """
    async_delete_issues(hass, entry.entry_id)
    # An entry whose setup kept failing was never unloaded, yet made the account.
    await _async_release_account(hass, entry)
    await Store(
        hass, COST_PRICES_STORAGE_VERSION, f"{COST_PRICES_STORAGE_KEY}.{entry.entry_id}"
    ).async_remove()
    username = entry.data[CONF_USERNAME]
    if not any(
        other.entry_id != entry.entry_id and other.data.get(CONF_USERNAME) == username
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await Store(hass, NOTIFICATIONS_STORAGE_VERSION, storage_key(username)).async_remove()
