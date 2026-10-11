"""A stand-in for the enea_prices integration, as the Enea integration sees it.

Only what Enea reaches for duck-typed is here: entries keyed by tariff group,
their runtime_data, the TARIFFS table and AKCYZA.  Like the real one, setting
an entry up reloads the Enea entries of its tariff group.
"""
from __future__ import annotations

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import const  # noqa: F401 - loaded with the package, as the real one's is
from .tariffs import TARIFFS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Price the entry's tariff group and reload the Enea meters in it."""
    entry.runtime_data = SimpleNamespace(
        tariff=TARIFFS[entry.data["tariff"]],
        phases=entry.data["phases"],
        annual_kwh=entry.data["annual_kwh"],
        billing_months=entry.data["billing_months"],
    )
    wanted = entry.data["tariff"].casefold()
    for meter in hass.config_entries.async_entries("enea"):
        coordinator = getattr(getattr(meter, "runtime_data", None), "coordinator", None)
        if coordinator is not None and (coordinator.tariff_name or "").casefold() == wanted:
            hass.config_entries.async_schedule_reload(meter.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nothing to tear down."""
    return True
