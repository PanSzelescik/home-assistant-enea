"""The suite must be able to import the integration at all."""
from __future__ import annotations


def test_modules_import() -> None:
    """Integration modules import without a running Home Assistant."""
    from custom_components.enea import billing, costs, statistics

    assert callable(costs.find_tariff_group)
    assert callable(billing.find_prices_config)
    assert callable(statistics.get_statistic_id)


def test_cost_statistic_name_is_stable() -> None:
    """Cost statistic names are part of the stored statistic_id and must not drift."""
    from custom_components.enea.costs import get_cost_statistic_name

    assert get_cost_statistic_name("pobrana", "peak") == "Koszt energii pobrana – Szczyt"
    assert (
        get_cost_statistic_name("oddana", "off_peak")
        == "Koszt energii oddana – Poza szczytem"
    )
    # G12sezON i G13active z enea_prices
    assert (
        get_cost_statistic_name("pobrana", "recommended_use")
        == "Koszt energii pobrana – Zalecany pobór"
    )
    assert (
        get_cost_statistic_name("pobrana", "remaining")
        == "Koszt energii pobrana – Pozostałe godziny"
    )
    assert (
        get_cost_statistic_name("pobrana", "recommended_limit")
        == "Koszt energii pobrana – Zalecane ograniczenie"
    )
