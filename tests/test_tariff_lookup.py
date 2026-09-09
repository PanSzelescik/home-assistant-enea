"""Matching the tariff group reported by the portal against the configured one."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from conftest import FakeConfigEntry, FakeHass

from custom_components.enea.billing import find_prices_config
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
