"""A prosumer's net metering (system opustów): the bill, the costs and the option."""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.enea import billing, config_flow, costs
from custom_components.enea.billing import PricesConfig, async_estimate_bill, settle_net_metering
from custom_components.enea.const import STAT_KEY_ENERGY_RETURNED
from custom_components.enea.statistics import get_statistic_id

TZ = datetime.UTC
START = datetime.date(2026, 3, 1)
END = datetime.date(2026, 3, 31)


def test_returned_energy_settles_its_own_zone_first() -> None:
    """Both zones have enough consumption: each takes only its own credit."""
    billed, left = settle_net_metering(
        {"Szczyt": 100.0, "Poza szczytem": 300.0},
        {"Szczyt": 50.0, "Poza szczytem": 100.0},
        0.8,
        ["Szczyt", "Poza szczytem"],
    )

    assert billed == {"Szczyt": 60.0, "Poza szczytem": 220.0}
    assert left == 0.0


def test_a_zones_surplus_goes_to_the_other_zone() -> None:
    """Midday production exceeds peak consumption; the rest lowers off-peak."""
    billed, left = settle_net_metering(
        {"Szczyt": 100.0, "Poza szczytem": 300.0},
        {"Szczyt": 500.0, "Poza szczytem": 0.0},
        0.8,
        ["Szczyt", "Poza szczytem"],
    )

    # 500 x 0.8 = 400: 100 settle peak, the other 300 settle off-peak.
    assert billed == {"Szczyt": 0.0, "Poza szczytem": 0.0}
    assert left == 0.0


def test_a_surplus_goes_to_the_highest_variable_rate_first() -> None:
    """With three zones the surplus follows the order it is given."""
    billed, left = settle_net_metering(
        {"Szczyt": 0.0, "Dzień": 100.0, "Noc": 100.0},
        {"Szczyt": 150.0},
        0.8,
        ["Dzień", "Noc", "Szczyt"],
    )

    assert billed == {"Szczyt": 0.0, "Dzień": 0.0, "Noc": 80.0}
    assert left == 0.0


def test_credit_beyond_all_consumption_is_left_over() -> None:
    billed, left = settle_net_metering({"Dzień": 100.0}, {"Dzień": 200.0}, 0.7, ["Dzień"])

    assert billed == {"Dzień": 0.0}
    assert left == pytest.approx(40.0)


class _Pricing:
    """One zone's per-kWh netto prices."""

    def __init__(self, variable_network: float) -> None:
        self.energy = 0.5
        self.variable_network = variable_network
        self.quality = 0.03
        self.oze = 0.007
        self.cogeneration = 0.003


class _Monthly:
    """Fixed monthly fees, independent of the installation here."""

    def get_network_fixed(self, phases: int) -> float:
        return 20.0

    def get_capacity(self, annual_kwh: int) -> float:
        return 10.0

    def get_subscription(self, billing_months: int) -> float:
        return 1.0


class _Period:
    """A G12w period: peak has the higher variable network rate."""

    def __init__(self) -> None:
        self.zones = {"peak": _Pricing(0.3), "off_peak": _Pricing(0.1)}
        self.monthly = _Monthly()


class _Tariff:
    name = "G12w"

    def get_period_for_date(self, d: datetime.date) -> _Period:
        return _Period()


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch):
    """Serve each statistic's total at the start and at the end of the period."""

    def _wire(totals: dict[str, tuple[float, float]]) -> None:
        sums = {get_statistic_id("PPE", name): pair for name, pair in totals.items()}

        class Rec:
            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                return target(*args)

        def during(hass: Any, start: Any, end: Any, ids: set, *rest: Any) -> dict:
            sid = next(iter(ids))
            if sid not in sums:
                return {}
            opening, closing = sums[sid]
            return {
                sid: [
                    {"start": _midnight(START).timestamp(), "sum": opening},
                    {"start": _midnight(END).timestamp(), "sum": closing},
                ]
            }

        monkeypatch.setattr(billing, "get_instance", lambda hass: Rec())
        monkeypatch.setattr(billing, "statistics_during_period", during)

    return _wire


def _midnight(d: datetime.date) -> datetime.datetime:
    return datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)


def _cfg() -> PricesConfig:
    return PricesConfig(
        tariff=_Tariff(), phases=3, annual_kwh=5000, billing_months=1, akcyza=0.005
    )


TOTALS = {
    "Energia pobrana – Szczyt": (1000.0, 1100.0),  # 100 kWh
    "Energia pobrana – Pozaszczyt": (2000.0, 2300.0),  # 300 kWh
    "Energia oddana – Szczyt": (500.0, 1000.0),  # 500 kWh
    "Energia oddana – Pozaszczyt": (0.0, 0.0),
}


async def test_the_bill_charges_only_what_net_metering_leaves(stored) -> None:
    """Energy and the variable fees fall on the rest; the fixed fees stay."""
    stored(TOTALS)

    with_ratio = await async_estimate_bill(object(), "PPE", _cfg(), START, END, 0.7)
    without = await async_estimate_bill(object(), "PPE", _cfg(), START, END)

    assert with_ratio is not None and without is not None
    # 500 x 0.7 = 350: 100 settle peak, 250 of the 300 off-peak.
    assert with_ratio.kwh_by_zone == pytest.approx({"Szczyt": 100.0, "Poza szczytem": 300.0})
    assert with_ratio.returned_kwh_by_zone == pytest.approx({"Szczyt": 500.0, "Poza szczytem": 0.0})
    assert with_ratio.billed_kwh_by_zone == pytest.approx({"Szczyt": 0.0, "Poza szczytem": 50.0})
    assert with_ratio.net_metering_left_kwh == 0.0
    assert with_ratio.energy_by_zone_netto == {"Szczyt": 0.0, "Poza szczytem": 25.25}
    assert with_ratio.variable_network_by_zone_netto == {"Szczyt": 0.0, "Poza szczytem": 5.0}
    assert with_ratio.fixed_network_netto == without.fixed_network_netto
    assert with_ratio.total < without.total


async def test_without_net_metering_the_bill_is_unchanged(stored) -> None:
    stored(TOTALS)

    estimate = await async_estimate_bill(object(), "PPE", _cfg(), START, END)

    assert estimate is not None
    assert estimate.returned_kwh_by_zone == {}
    assert estimate.billed_kwh_by_zone == {}
    assert estimate.net_metering_left_kwh is None
    assert estimate.energy_by_zone_netto == {"Szczyt": 50.5, "Poza szczytem": 151.5}


class _CostPricing:
    energy = 0.5
    total_distribution = 0.3


class _CostPeriod:
    def __init__(self) -> None:
        self.zones = {"peak": _CostPricing()}

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        return "peak"


class _CostTariff:
    name = "G12w"

    def __init__(self) -> None:
        self.periods = [SimpleNamespace(valid_from=datetime.date(2026, 1, 1))]

    def get_period_for_date(self, d: datetime.date) -> _CostPeriod:
        return _CostPeriod()


async def test_returned_energy_is_costed_at_the_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A returned kWh is worth only the ratio of a consumed one."""
    injected: list[tuple[str, list]] = []

    async def fake_inject(hass: Any, meter_code: str, name: str, series: list) -> float:
        injected.append((name, series))
        return 0.0

    monkeypatch.setattr(costs, "_inject_cost_series", fake_inject)
    day = datetime.date(2026, 3, 2)
    end = datetime.datetime(2026, 3, 2, 12, tzinfo=TZ)
    days = [
        (
            day,
            {
                STAT_KEY_ENERGY_RETURNED: {
                    "values": [{"integrationEnd": end.timestamp() * 1000, "items": [{"value": 2.0}]}]
                }
            },
        )
    ]

    await costs.async_insert_cost_statistics(object(), "PPE", days, _CostTariff(), False, True, 0.8)

    # 2 kWh x (0.5 + 0.3) x 1.23 VAT x 0.8 = 1.5744 zl
    assert injected[0][0] == "Koszt energii oddana – Szczyt"
    assert injected[0][1][0][1] == pytest.approx(1.5744)


def test_a_new_ratio_reprices_but_none_keeps_the_stored_fingerprints() -> None:
    """Fingerprints stored before the option existed must still match without it."""
    day = datetime.date(2026, 3, 2)
    tariff = _CostTariff()

    plain = costs.price_signatures(tariff, day, day)

    assert costs.price_signatures(tariff, day, day, 1.0) == plain
    assert costs.price_signatures(tariff, day, day, 0.8) != plain
    assert costs.price_signatures(tariff, day, day, 0.8) != costs.price_signatures(
        tariff, day, day, 0.7
    )


def _options_flow(options: dict[str, Any], prosumer: bool | None) -> config_flow.EneaOptionsFlow:
    """An options flow over an entry, loaded with a coordinator unless prosumer is None."""
    entry = SimpleNamespace(options=options)
    if prosumer is not None:
        entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(prosumer=prosumer))
    flow = config_flow.EneaConfigFlow.async_get_options_flow(entry)
    flow.handler = "entry"
    flow.flow_id = "options-flow"
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_known_entry=Mock(return_value=entry))
    )
    return flow


OPTIONS = {
    "update_interval": {"hours": 3, "minutes": 30},
    "fetch_consumption": True,
    "fetch_generation": True,
    "fetch_power_consumption": False,
    "fetch_power_generation": False,
}


@pytest.mark.parametrize(
    ("options", "prosumer", "asked"),
    [
        pytest.param(OPTIONS, True, True, id="prosumer"),
        pytest.param(OPTIONS, False, False, id="consumer"),
        pytest.param(OPTIONS, None, False, id="not-loaded"),
        pytest.param({**OPTIONS, "net_metering": "0_8"}, None, True, id="set-before"),
    ],
)
async def test_only_a_prosumer_is_asked_for_the_ratio(options, prosumer, asked) -> None:
    result = await _options_flow(options, prosumer).async_step_init()

    assert ("net_metering" in result["data_schema"].schema) is asked
    if asked:
        assert result["data_schema"]({})["net_metering"] == options.get("net_metering", "none")


async def test_a_prosumer_sets_the_ratio_on_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last setup step asks a prosumer too, and stores the choice."""
    flow = config_flow.EneaConfigFlow()
    flow.handler = "enea"
    flow.flow_id = "test-flow"
    flow.context = {"source": "user"}
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entry_for_domain_unique_id=Mock(return_value=None))
    )
    monkeypatch.setattr(flow, "_async_in_progress", Mock(return_value=[]))
    flow._username, flow._password = "test@example.invalid", "test-password"
    flow._selected_meter = {
        "id": 12345,
        "code": "590310600000001234",
        "type": 2,
        "tariffGroup": {"name": "G12W"},
    }

    form = await flow.async_step_configure()
    result = await flow.async_step_configure({**OPTIONS, "net_metering": "0_7"})

    assert "net_metering" in form["data_schema"].schema
    assert result["type"] == "create_entry"
    assert result["options"]["net_metering"] == "0_7"
