"""Enea Energy Meter integration for Home Assistant."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .connector import EneaApiClient
from .const import CONF_BACKFILL_DAYS, CONF_METER_ID, CONF_METER_NAME, CONF_UPDATE_INTERVAL, DEFAULT_BACKFILL_DAYS, DEFAULT_UPDATE_INTERVAL_DICT, DOMAIN, PLATFORMS
from .coordinator import EneaUpdateCoordinator

SERVICE_REFRESH = "refresh"


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
        connector: EneaApiClient = hass.data[DOMAIN][username]
        connector.update_credentials(password)
    else:
        session = async_create_clientsession(hass)
        connector = EneaApiClient(session, username, password)
        hass.data[DOMAIN][username] = connector

    duration = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_DICT)
    update_interval = timedelta(
        hours=int(duration.get("hours", 0)),
        minutes=int(duration.get("minutes", 0)),
        seconds=int(duration.get("seconds", 0)),
    )
    coordinator = EneaUpdateCoordinator(
        hass,
        connector,
        entry.data[CONF_METER_ID],
        meter_code=entry.data[CONF_METER_NAME],
        backfill_days=entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
        update_interval=update_interval,
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = EneaRuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
                meter_code = config_entry.data.get(CONF_METER_NAME)
                device = dev_reg.async_get_device(
                    identifiers={(DOMAIN, meter_code)}
                )
                if device and (not device_ids or device.id in device_ids):
                    await config_entry.runtime_data.coordinator.async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _handle_refresh)

    return True


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

    return unload_ok
