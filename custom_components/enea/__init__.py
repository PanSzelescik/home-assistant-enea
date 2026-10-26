"""Enea Energy Meter integration for Home Assistant."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient
from .const import (
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
    NET_METERING_RATIOS,
    NOTIFICATIONS_STORAGE_VERSION,
    PLATFORMS,
    SERVICE_BACKFILL,
    SERVICE_REFRESH,
)
from .coordinator import EneaUpdateCoordinator
from .issues import async_delete_issues
from .notifications import EneaNotifications, storage_key


@dataclass
class EneaAccount:
    """What the meters of one Portal Odbiorcy Enea account share."""

    client: EneaApiClient
    notifications: EneaNotifications


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
    device_ids: list[str] = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    dev_reg = dr.async_get(hass)
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        if not hasattr(config_entry, "runtime_data"):
            continue
        devices = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
        if devices and (not device_ids or any(device.id in device_ids for device in devices)):
            yield config_entry.runtime_data.coordinator


async def async_setup_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Set up Enea from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Reuse existing client for accounts with multiple meters (shared session/cookie).
    # If credentials changed (e.g. after reauth), update them in the shared client.
    # The messages are checked per account too: one request for all its meters.
    account: EneaAccount | None = hass.data[DOMAIN].get(username)
    if account is not None and not account.client.session_closed:
        account.client.update_credentials(password)
    else:
        if account is not None:
            account.notifications.unsubscribe()
        session = async_create_clientsession(hass)
        client = EneaApiClient(session, username, password)
        account = EneaAccount(client, EneaNotifications(hass, client, username))
        hass.data[DOMAIN][username] = account

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
    entry.async_on_unload(update_coordinator.cancel_backfill)
    await update_coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the refresh service once for the whole domain.
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):

        async def _handle_refresh(call: ServiceCall) -> None:
            """Handle the 'enea.refresh' service call.

            Triggers an immediate data refresh for each matching device.
            If no device_id is provided, all Enea devices are refreshed.
            """
            for coordinator in _matching_coordinators(hass, call):
                await coordinator.async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _handle_refresh)

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):

        async def _handle_backfill(call: ServiceCall) -> None:
            """Handle the 'enea.backfill' service call.

            Imports historical statistics for the specified date range.
            Accepts start_date/end_date (ISO format), days_back, or defaults
            to the last 30 days when no parameters are given.
            """
            today = dt_util.now().date()
            yesterday = today - timedelta(days=1)

            start_date_str: str | None = call.data.get("start_date")
            end_date_str: str | None = call.data.get("end_date")
            days_back: int | None = call.data.get("days_back")

            if start_date_str:
                start_date = dt_util.parse_date(start_date_str)
                end_date = dt_util.parse_date(end_date_str) if end_date_str else yesterday
            elif days_back:
                end_date = yesterday
                start_date = yesterday - timedelta(days=int(days_back) - 1)
            else:
                end_date = yesterday
                start_date = yesterday - timedelta(days=29)  # default: last 30 days

            for coordinator in _matching_coordinators(hass, call):
                await coordinator.async_backfill(start_date, end_date)

        hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle_backfill)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        username = entry.data[CONF_USERNAME]
        # Only loaded entries count: one unloaded earlier (disabled, failed to set
        # up) uses neither the client nor the actions.
        remaining = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
        ]
        # Remove the shared client and messages only when no other entries use the same account.
        if (
            not any(e.data.get(CONF_USERNAME) == username for e in remaining)
            and (account := hass.data[DOMAIN].pop(username, None)) is not None
        ):
            account.notifications.unsubscribe()

        # Remove the service when the last entry is unloaded.
        if not remaining:
            hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> None:
    """Delete the repair issues and the stores of a removed meter entry.

    The messages seen are the account's: they go with its last meter.
    """
    async_delete_issues(hass, entry.entry_id)
    await Store(
        hass, COST_PRICES_STORAGE_VERSION, f"{COST_PRICES_STORAGE_KEY}.{entry.entry_id}"
    ).async_remove()
    username = entry.data[CONF_USERNAME]
    if not any(
        other.entry_id != entry.entry_id and other.data.get(CONF_USERNAME) == username
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await Store(hass, NOTIFICATIONS_STORAGE_VERSION, storage_key(username)).async_remove()
