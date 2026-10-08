"""Diagnostics support for the Enea Energy Meter integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import EneaConfigEntry
from .billing import find_prices_config
from .connector import infer_phases
from .const import CONF_METER_ID, CONF_METER_NAME
from .statistics import async_statistics_overview

# Dane pozwalające zidentyfikować odbiorcę — raport bywa wklejany w publiczne zgłoszenia,
# a te może czytać także Enea.  Klucz "code" i CONF_METER_NAME to numer PPE,
# CONF_METER_ID — wewnętrzne ID licznika w Portalu Odbiorcy Enea, "serialNumber" — numery
# liczników, "agreementNumber" — numery umów.
TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_ADDRESS,
    CONF_METER_ID,
    CONF_METER_NAME,
    "code",
    "serialNumber",
    "agreementNumber",
}

# Klucze "name" i "id" są ukrywane tylko tam, gdzie identyfikują odbiorcę — na najwyższym
# poziomie danych licznika (numer PPE, ID licznika w portalu) i w listach liczników
# fizycznych i umów.  Głębiej to nazwy i ID stref taryfowych, wspólne dla wszystkich
# odbiorców taryfy i potrzebne do diagnozy.
_REDACT_TOP_LEVEL = ("name", "id")
_REDACT_ID_IN_LISTS = ("meters", "agreements")


def _redact_meter_data(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the dashboard data with everything identifying the customer hidden."""
    redacted = async_redact_data(data, TO_REDACT)
    if not redacted:
        return redacted
    for key in _REDACT_TOP_LEVEL:
        if redacted.get(key) is not None:
            redacted[key] = REDACTED
    for list_key in _REDACT_ID_IN_LISTS:
        redacted[list_key] = [
            {**item, "id": REDACTED} if isinstance(item, dict) and "id" in item else item
            for item in redacted.get(list_key) or []
        ]
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EneaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Triggers a fresh data fetch so the diagnostics always reflect the current
    state from the Portal Odbiorcy Enea (not a potentially stale cached value).
    Home Assistant adds its own version, the time zone, the versions of custom
    integrations (enea_prices included) and open repair issues by itself.
    """
    coordinator = entry.runtime_data.coordinator

    await coordinator.async_refresh()

    data = coordinator.data or {}
    phases, phases_source = infer_phases(data)
    prices = find_prices_config(hass, data.get("tariffGroupName"))

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
            **coordinator.diagnostics_state(),
        },
        "phases": {
            "inferred": phases,
            "source": phases_source,
            "enea_prices": prices.phases if prices is not None else None,
        },
        "enea_prices": None if prices is None else {
            "tariff": getattr(prices.tariff, "name", None),
            "billing_months": prices.billing_months,
            "annual_kwh": prices.annual_kwh,
            "akcyza": prices.akcyza,
        },
        "statistics": await async_statistics_overview(hass, entry.data[CONF_METER_NAME]),
        "meter_data": _redact_meter_data(coordinator.data),
    }
