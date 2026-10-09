"""Matching the tariff group reported by the portal against the configured one."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from conftest import FakeConfigEntry, FakeHass

from custom_components.enea.billing import find_prices_config
from custom_components.enea.connector import agreement_tariffs, tariff_group_name, zone_name
from custom_components.enea.costs import find_tariff_group


@dataclass
class FakeRuntime:
    """enea_prices runtime data, reached by duck typing."""

    tariff: Any
    phases: int = 3
    annual_kwh: int = 5000
    billing_months: int = 2


def _hass(configured: str) -> FakeHass:
    """Return a hass with a single enea_prices entry for the given tariff key."""
    return FakeHass(
        [
            FakeConfigEntry(
                domain="enea_prices",
                data={"tariff": configured},
                runtime_data=FakeRuntime(tariff="TARIFF_OBJECT"),
            )
        ]
    )


# The portal reports "G12W"; enea_prices stores the TARIFFS key "G12w".
@pytest.mark.parametrize(
    ("configured", "reported"),
    [
        ("G12w", "G12W"),  # the combination seen in practice
        ("G12W", "G12w"),  # and the same mismatch the other way round
        ("G12w", "G12w"),  # exact match must keep working
        ("G11", "G11"),
    ],
)
def test_tariff_found_regardless_of_case(configured: str, reported: str) -> None:
    """A tariff differing only in letter case is still matched."""
    assert find_tariff_group(_hass(configured), reported) == "TARIFF_OBJECT"


@pytest.mark.parametrize(
    ("configured", "reported"),
    [
        ("G11", "G12w"),  # different groups must not be conflated
        ("G12", "G12w"),  # a prefix is not a match either
        ("G12w", ""),
        ("G12w", None),
    ],
)
def test_different_tariff_is_not_matched(configured: str, reported: str | None) -> None:
    """Only case may differ — anything else is a different tariff group."""
    assert find_tariff_group(_hass(configured), reported) is None


def test_missing_tariff_key_is_not_matched() -> None:
    """An entry without a tariff key must not match an empty comparison."""
    hass = FakeHass(
        [
            FakeConfigEntry(
                domain="enea_prices", data={}, runtime_data=FakeRuntime(tariff="X")
            )
        ]
    )
    assert find_tariff_group(hass, "G12w") is None


def test_prices_config_found_regardless_of_case() -> None:
    """find_prices_config shares the comparison and must behave the same."""
    cfg = find_prices_config(_hass("G12w"), "G12W")

    assert cfg is not None
    assert cfg.tariff == "TARIFF_OBJECT"
    assert cfg.phases == 3
    assert cfg.billing_months == 2


def test_prices_config_not_found_for_other_tariff() -> None:
    """A different group yields no configuration."""
    assert find_prices_config(_hass("G11"), "G12w") is None


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("G12sez", "G12sezON"),  # issue #6 in enea_prices: the invoice says G12sezON
        ("G12SEZ", "G12sezON"),
        (" G12sez ", "G12sezON"),
        ("G12sezON", "G12sezON"),
        ("G12W", "G12W"),  # other names pass through; lookups ignore case anyway
        ("", ""),
        (None, ""),
    ],
)
def test_tariff_group_name_as_on_the_invoice(reported: str | None, expected: str) -> None:
    """The portal's shortened group names map to the ones enea_prices knows."""
    assert tariff_group_name(reported) == expected


def test_shortened_group_finds_its_prices() -> None:
    """A meter reporting "G12sez" is priced by the "G12sezON" entry."""
    assert find_tariff_group(_hass("G12sezON"), tariff_group_name("G12sez")) == "TARIFF_OBJECT"


def test_agreements_use_the_invoice_group_name() -> None:
    """Agreements name their group the portal's way too."""
    data = {"agreements": [{"from": 1781906400000, "to": None, "tariffGroupName": "G12sez"}]}

    assert [group for _, _, group in agreement_tariffs(data)] == ["G12sezON"]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Dzień 1.8.1", "Dzień"),
        ("Poza szczytem 1.8.2", "Poza szczytem"),
        ("Strefa zalecanego poboru 1.8.2", "Strefa zalecanego poboru"),
        ("Pozostałe godziny doby 1.8.1", "Pozostałe godziny doby"),
        ("Bezstrefowo", "Bezstrefowo"),
    ],
)
def test_zone_name_drops_only_the_obis_code(label: str, expected: str) -> None:
    """A zone sensor keeps the whole zone name, not just its first word."""
    assert zone_name(label) == expected
