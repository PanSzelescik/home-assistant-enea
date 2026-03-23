"""Enea Energy Meter integration for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

_LOGGER = logging.getLogger(__name__)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .connector import EneaApiClient
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_ID,
    CONF_METER_NAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_UPDATE_INTERVAL_DICT,
    DOMAIN,
    PLATFORMS,
    SERVICE_BACKFILL,
    SERVICE_REFRESH,
)
from .coordinator import EneaUpdateCoordinator

@dataclass
class EneaRuntimeData:
    """Runtime data stored in the config entry."""

    coordinator: EneaUpdateCoordinator


type EneaConfigEntry = ConfigEntry[EneaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Set up Enea from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Reuse existing client for accounts with multiple meters (shared session/cookie).
    # If credentials changed (e.g. after reauth), update them in the shared client.
    if username in hass.data[DOMAIN]:
        client: EneaApiClient = hass.data[DOMAIN][username]
        client.update_credentials(password)
    else:
        session = async_create_clientsession(hass)
        client = EneaApiClient(session, username, password)
        hass.data[DOMAIN][username] = client

    duration = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT)
    update_interval = timedelta(
        hours=int(duration.get("hours", 0)),
        minutes=int(duration.get("minutes", 0)),
        seconds=int(duration.get("seconds", 0)),
    )
    update_coordinator = EneaUpdateCoordinator(
        hass,
        client,
        entry.data[CONF_METER_ID],
        meter_code=entry.data[CONF_METER_NAME],
        backfill_days=entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
        update_interval=update_interval,
        fetch_consumption=entry.options.get(CONF_FETCH_CONSUMPTION, True),
        fetch_generation=entry.options.get(CONF_FETCH_GENERATION, True),
    )
    await update_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = EneaRuntimeData(coordinator=update_coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Cost sensor entities are now registered; inject any pending cost statistics.
    try:
        await update_coordinator.async_setup_costs()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to set up cost statistics: %s", err)

    # Register the refresh service once for the whole domain.
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):

        async def _handle_refresh(call: ServiceCall) -> None:
            device_ids: list[str] = call.data.get("device_id", [])
            if isinstance(device_ids, str):
                device_ids = [device_ids]

            dev_reg = dr.async_get(hass)
            for config_entry in hass.config_entries.async_entries(DOMAIN):
                if not hasattr(config_entry, "runtime_data"):
                    continue
                meter_code: str = config_entry.data[CONF_METER_NAME]
                device = dev_reg.async_get_device(
                    identifiers={(DOMAIN, meter_code)}
                )
                if device and (not device_ids or device.id in device_ids):
                    await config_entry.runtime_data.coordinator.async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _handle_refresh)

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):

        async def _handle_backfill(call: ServiceCall) -> None:
            device_ids: list[str] = call.data.get("device_id", [])
            if isinstance(device_ids, str):
                device_ids = [device_ids]

            today = dt_util.now().date()
            yesterday = today - timedelta(days=1)

            start_date_str: str | None = call.data.get("start_date")
            end_date_str: str | None = call.data.get("end_date")
            days_back: int | None = call.data.get("days_back")

            if start_date_str:
                start_date = date.fromisoformat(start_date_str)
                end_date = date.fromisoformat(end_date_str) if end_date_str else yesterday
            elif days_back:
                end_date = yesterday
                start_date = yesterday - timedelta(days=int(days_back) - 1)
            else:
                end_date = yesterday
                start_date = yesterday - timedelta(days=DEFAULT_BACKFILL_DAYS - 1)

            dev_reg = dr.async_get(hass)
            for config_entry in hass.config_entries.async_entries(DOMAIN):
                if not hasattr(config_entry, "runtime_data"):
                    continue
                meter_code: str = config_entry.data[CONF_METER_NAME]
                device = dev_reg.async_get_device(
                    identifiers={(DOMAIN, meter_code)}
                )
                if device and (not device_ids or device.id in device_ids):
                    await config_entry.runtime_data.coordinator.async_backfill(
                        start_date, end_date
                    )

        hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle_backfill)

    return True


async def _async_update_options(hass: HomeAssistant, entry: EneaConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EneaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        username = entry.data[CONF_USERNAME]
        # Remove the shared client only when no other entries use the same account.
        other_entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
            and e.data.get(CONF_USERNAME) == username
        ]
        if not other_entries:
            hass.data[DOMAIN].pop(username, None)

        # Remove the service when the last entry is unloaded.
        remaining = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not remaining:
            hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)

    return unload_ok
