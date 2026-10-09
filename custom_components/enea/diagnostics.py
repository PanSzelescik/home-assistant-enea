"""Diagnostics support for the Enea Energy Meter integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import EneaConfigEntry
from .billing import find_prices_config
from .const import CONF_METER_ID, CONF_METER_NAME, SENSOR_KEY_ADDRESS
from .installation import DetectedInstallation
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

# billingWeekData niesie też cztery kwadranty energii biernej, których integracja nie
# używa, a które zajmują większość raportu — zostaje tylko energia czynna.
_ACTIVE_ENERGY_MEASUREMENT_IDS = (1, 2)


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
    redacted["billingWeekData"] = [
        measurement
        for measurement in redacted.get("billingWeekData") or []
        if measurement.get("measurementId") in _ACTIVE_ENERGY_MEASUREMENT_IDS
    ]
    return redacted


def _entity_states(hass: HomeAssistant, entry: EneaConfigEntry) -> dict[str, Any]:
    """Return what Home Assistant shows for each entity of the entry.

    Keyed by the entity key rather than the entity id, which carries the PPE
    number; the friendly name is left out for the same reason.  Comparing these
    states with the coordinator section shows at once when an entity does not
    follow its data.
    """
    prefix = f"enea-{entry.data[CONF_METER_NAME]}-"
    entities: dict[str, Any] = {}
    for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        key = reg.unique_id.removeprefix(prefix)
        state = hass.states.get(reg.entity_id)
        hidden = key == SENSOR_KEY_ADDRESS
        attributes = (
            {k: v for k, v in state.attributes.items() if k != "friendly_name"}
            if state is not None
            else {}
        )
        entities[key] = {
            "domain": reg.domain,
            "disabled_by": reg.disabled_by.value if reg.disabled_by else None,
            "state": None if state is None else (REDACTED if hidden else state.state),
            "last_changed": state.last_changed.isoformat() if state is not None else None,
            "attributes": REDACTED if hidden else async_redact_data(attributes, TO_REDACT),
        }
    return entities


def _prices_coverage(tariff: Any) -> dict[str, Any]:
    """Return the date ranges the enea_prices tariff table can price.

    The bundled table ends on a fixed date; after it costs silently stop
    growing, which this makes visible.
    """
    periods = [
        (getattr(period, "valid_from", None), getattr(period, "valid_until", None))
        for period in getattr(tariff, "periods", [])
    ]
    ends = [until for _, until in periods if until is not None]
    return {
        "covered_until": max(ends).isoformat() if ends else None,
        "periods": [
            [start.isoformat() if start else None, until.isoformat() if until else None]
            for start, until in periods
        ],
    }


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

    prices = find_prices_config(hass, coordinator.tariff_name)

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
            **coordinator.diagnostics_state(),
        },
        "installation": {
            "detected": getattr(
                coordinator, "detected_installation", DetectedInstallation()
            ).as_dict(),
            "enea_prices": None if prices is None else {
                "phases": prices.phases,
                "billing_months": prices.billing_months,
                "annual_kwh": prices.annual_kwh,
            },
        },
        "enea_prices": None if prices is None else {
            "tariff": getattr(prices.tariff, "name", None),
            "akcyza": prices.akcyza,
            **_prices_coverage(prices.tariff),
        },
        "entities": _entity_states(hass, entry),
        "statistics": await async_statistics_overview(hass, entry.data[CONF_METER_NAME]),
        "meter_data": _redact_meter_data(coordinator.data),
    }
