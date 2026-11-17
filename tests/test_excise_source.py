"""Where the bill takes the excise duty from: the enea_prices entry, then its const module."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from custom_components.enea.billing import find_prices_config
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    clock,
    database,
    hass,
    make_prices_entry,
    portal,
    register_prices_entry,
)

CONST_MODULE = "custom_components.enea_prices.const"


@pytest.fixture
def const_module(monkeypatch: pytest.MonkeyPatch):
    """Put a stand-in for enea_prices' const module where enea looks for it."""
    def install(**names: float) -> None:
        monkeypatch.setitem(sys.modules, CONST_MODULE, SimpleNamespace(**names))

    monkeypatch.delitem(sys.modules, CONST_MODULE, raising=False)
    return install


def _config(hass, **runtime: float):
    """Return the prices configuration of a G12 entry whose runtime data has these extras."""
    entry = make_prices_entry()
    for name, value in runtime.items():
        setattr(entry.runtime_data, name, value)
    register_prices_entry(hass, entry)
    config = find_prices_config(hass, "G12")
    assert config is not None
    return config


async def test_excise_on_the_entry_is_used(hass, const_module):
    """A newer enea_prices hands the excise out with its runtime data."""
    const_module(AKCYZA=0.005)

    assert _config(hass, akcyza=0.0039).akcyza == 0.0039


async def test_zero_excise_on_the_entry_is_taken_as_zero(hass, const_module):
    """An excise of 0.0 is a value, not a reason to look elsewhere."""
    const_module(AKCYZA=0.005)

    assert _config(hass, akcyza=0.0).akcyza == 0.0


async def test_older_prices_fall_back_to_the_const_module(hass, const_module):
    """An enea_prices that does not hand it out still has the constant."""
    const_module(AKCYZA=0.005)

    assert _config(hass).akcyza == 0.005


async def test_no_source_at_all_means_no_excise(hass, const_module):
    """Neither the entry nor a loaded module: the bill is computed without excise."""
    assert _config(hass).akcyza == 0.0


async def test_const_module_without_the_name_means_no_excise(hass, const_module):
    """A const module that lost the name does not break the lookup."""
    const_module(VAT_RATE=0.23)

    assert _config(hass).akcyza == 0.0
