"""Repair issues follow the meter data: raised while true, deleted once not."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea import issues as issues_module
from custom_components.enea import repairs as repairs_module
from custom_components.enea.coordinator import EneaUpdateCoordinator
from custom_components.enea.installation import DetectedInstallation

from conftest import FakeConfigEntry


@dataclass
class _Prices:
    """The part of PricesConfig the issues read."""

    phases: int = 3
    billing_months: int = 2
    annual_kwh: int = 5000


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """Record issue registry calls and let a test choose the enea_prices settings."""
    state: dict[str, Any] = {"open": {}, "prices": None}

    def create(hass, domain, issue_id, **kwargs):
        state["open"][issue_id] = kwargs

    def delete(hass, domain, issue_id):
        state["open"].pop(issue_id, None)

    monkeypatch.setattr(issues_module.ir, "async_create_issue", create)
    monkeypatch.setattr(issues_module.ir, "async_delete_issue", delete)
    monkeypatch.setattr(
        issues_module, "find_prices_config", lambda hass, tariff: state["prices"]
    )
    return state


def _dashboard(model: str | None, capacity: float | None = 14) -> dict[str, Any]:
    """Return a dashboard response with one active meter."""
    meters = [] if model is None else [{"typeName": model, "disassemblyDate": None}]
    return {"meters": meters, "agreementPower": capacity}


def _meter_entry(tariff: str, entry_id: str) -> FakeConfigEntry:
    """An Enea entry whose coordinator reports a tariff group."""
    coordinator = SimpleNamespace(tariff_name=tariff)
    return FakeConfigEntry(
        domain="enea", runtime_data=SimpleNamespace(coordinator=coordinator), entry_id=entry_id
    )


def _hass(language: str = "pl", meters: int = 1) -> Any:
    """Return a hass with the configured language and the given number of G12 meters."""
    entries = [_meter_entry("G12", f"entry{n + 1}") for n in range(meters)]
    return SimpleNamespace(
        config=SimpleNamespace(language=language),
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [e for e in entries if e.domain == domain]
        ),
    )


def _update(data: dict[str, Any], language: str = "pl") -> None:
    """Run one dashboard issue update for config entry "entry1" (PPE number "PPE")."""
    issues_module.async_update_issues(_hass(language), "entry1", "PPE", data)


THREE_PHASES_BILLED_BIMONTHLY = DetectedInstallation(
    phases=3,
    phases_source="meter_model",
    billing_months=2,
    billing_months_source="billing_periods",
    annual_kwh=3170.4,
    annual_kwh_source="billing_periods",
    annual_kwh_until=datetime.date(2026, 8, 5),
)


def _update_installation(
    detected: DetectedInstallation = THREE_PHASES_BILLED_BIMONTHLY, meters: int = 1
) -> None:
    """Run one installation issue update for config entry "entry1"."""
    issues_module.async_update_installation_issues(
        _hass(meters=meters), "entry1", "PPE", "G12", _dashboard("OTUS3"), detected
    )


def test_settings_matching_the_meter_raise_nothing(registry) -> None:
    registry["prices"] = _Prices()

    _update_installation()

    assert registry["open"] == {}


def test_phase_mismatch_is_raised_and_cleared(registry) -> None:
    """A wrong phase count in enea_prices is reported until it is fixed."""
    registry["prices"] = _Prices(phases=1)
    _update_installation()

    issue = registry["open"]["phases_mismatch_entry1"]
    assert issue["is_fixable"]
    assert issue["data"] == {"tariff": "G12", "setting": "phases", "value": 3}
    assert issue["translation_placeholders"]["detected"] == "3"
    assert issue["translation_placeholders"]["configured"] == "1"

    registry["prices"] = _Prices(phases=3)
    _update_installation()

    assert "phases_mismatch_entry1" not in registry["open"]


def test_billing_period_mismatch_offers_the_meters_period(registry) -> None:
    registry["prices"] = _Prices(billing_months=1)

    _update_installation()

    issue = registry["open"]["billing_months_mismatch_entry1"]
    assert issue["data"] == {"tariff": "G12", "setting": "billing_months", "value": 2}


def test_consumption_in_the_configured_bracket_is_no_mismatch(registry) -> None:
    """Only the bracket prices the capacity fee; 3170 kWh is in the one of 5000."""
    registry["prices"] = _Prices(annual_kwh=5000)

    _update_installation()

    assert "annual_kwh_mismatch_entry1" not in registry["open"]


def test_consumption_in_another_bracket_offers_the_measured_one(registry) -> None:
    registry["prices"] = _Prices(annual_kwh=2000)

    _update_installation()

    issue = registry["open"]["annual_kwh_mismatch_entry1"]
    assert issue["data"] == {"tariff": "G12", "setting": "annual_kwh", "value": 3170}
    assert issue["translation_placeholders"]["detected_bracket"] == "> 2800 kWh"
    assert issue["translation_placeholders"]["configured_bracket"] == "1200–2800 kWh"
    assert issue["translation_placeholders"]["until"] == "2026-08-05"


def test_nothing_is_raised_for_what_the_meter_leaves_open(registry) -> None:
    registry["prices"] = _Prices(phases=1, billing_months=1, annual_kwh=250)

    _update_installation(DetectedInstallation())

    assert registry["open"] == {}


def test_without_prices_nothing_is_compared(registry) -> None:
    _update_installation()

    assert registry["open"] == {}


def test_two_meters_in_one_group_raise_nothing(registry) -> None:
    """They share one enea_prices entry; fixing it for one breaks the other."""
    registry["prices"] = _Prices(phases=1)
    _update_installation()
    assert registry["open"]

    _update_installation(meters=2)

    assert registry["open"] == {}


def test_unknown_model_asks_for_a_report(registry) -> None:
    """An unknown model links to a pre-filled GitHub issue naming only the model."""
    _update(_dashboard("XYZ123"))

    issue = registry["open"]["unknown_meter_model_entry1"]
    url = urlsplit(issue["learn_more_url"])
    assert url.path.endswith("/issues/new")
    assert parse_qs(url.query) == {
        "template": ["new_meter_model_pl.yml"],
        "title": ["Nowy model licznika: XYZ123"],
        "model": ["XYZ123"],
        "capacity": ["14"],
    }
    assert issue["translation_placeholders"]["report_url"] == issue["learn_more_url"]

    _update(_dashboard("OTUS1"))

    assert "unknown_meter_model_entry1" not in registry["open"]


def test_unknown_model_report_follows_the_language(registry) -> None:
    """Outside Polish the English form opens; a missing capacity is left out."""
    _update(_dashboard("XYZ123", capacity=None), language="en")

    issue = registry["open"]["unknown_meter_model_entry1"]
    assert parse_qs(urlsplit(issue["learn_more_url"]).query) == {
        "template": ["new_meter_model_en.yml"],
        "title": ["New meter model: XYZ123"],
        "model": ["XYZ123"],
    }


def test_missing_meter_is_not_an_unknown_model(registry) -> None:
    """Without an active meter there is no model to report."""
    _update(_dashboard(None))

    assert registry["open"] == {}


def test_delete_issues_removes_everything(registry) -> None:
    """Removing the config entry leaves no issue of that meter behind."""
    registry["prices"] = _Prices(phases=1, billing_months=1, annual_kwh=250)
    _update(_dashboard("XYZ123"))
    _update_installation()
    assert len(registry["open"]) == 4

    issues_module.async_delete_issues(_hass(), "entry1")

    assert registry["open"] == {}


async def test_the_fix_writes_the_meters_value_into_enea_prices(monkeypatch) -> None:
    """Confirming sets the one setting, keeps the rest and reloads enea_prices."""
    prices = FakeConfigEntry(
        domain="enea_prices",
        data={"tariff": "G12", "phases": 1, "annual_kwh": 2000, "billing_months": 2},
        entry_id="prices",
    )
    calls: dict[str, Any] = {"reloaded": []}

    async def reload(entry_id: str) -> None:
        calls["reloaded"].append(entry_id)

    def update_entry(entry: FakeConfigEntry, *, data: dict[str, Any]) -> None:
        entry.data = data

    flow = await repairs_module.async_create_fix_flow(
        None, "phases_mismatch_entry1", {"tariff": "g12", "setting": "phases", "value": 3}
    )
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [prices] if domain == "enea_prices" else [],
            async_update_entry=update_entry,
            async_reload=reload,
        )
    )
    flow.handler = "enea"
    flow.flow_id = "fix"

    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert prices.data == {"tariff": "G12", "phases": 3, "annual_kwh": 2000, "billing_months": 2}
    assert calls["reloaded"] == ["prices"]


async def test_the_fix_aborts_once_enea_prices_is_gone() -> None:
    flow = await repairs_module.async_create_fix_flow(
        None, "phases_mismatch_entry1", {"tariff": "G12", "setting": "phases", "value": 3}
    )
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_entries=lambda domain: [])
    )
    flow.handler = "enea"
    flow.flow_id = "fix"

    result = await flow.async_step_confirm({})

    assert result["type"] == "abort"
    assert result["reason"] == "prices_entry_missing"


async def test_latest_statistics_date(wire_recorder) -> None:
    """The statistics date is the local day of the newest stored hour."""
    coord = object.__new__(EneaUpdateCoordinator)
    coord.hass = object()  # type: ignore[assignment]
    coord._meter_code = "PPE"
    coord._fetch_consumption = True
    coord._fetch_generation = False
    coord._fetch_power_consumption = False
    coord._fetch_power_generation = False
    newest = datetime.datetime(2026, 10, 7, 23, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    wire_recorder(coordinator_module, [(newest, 1.0)])

    assert await coord._async_latest_statistics_date() == datetime.date(2026, 10, 7)


class _Integration:
    """A custom integration as the loader lists it, its package already importable."""

    pkg_path = "fake_enea_prices"

    async def async_get_component(self) -> Any:
        return object()


@pytest.fixture
def prices_installed(monkeypatch: pytest.MonkeyPatch):
    """Make enea_prices installed, pricing G12 and G12w; the test may uninstall it."""
    import sys

    installed = {"enea_prices": _Integration()}

    async def custom_components(hass: Any) -> dict[str, Any]:
        return installed

    monkeypatch.setattr(issues_module, "async_get_custom_components", custom_components)
    monkeypatch.setitem(
        sys.modules, "fake_enea_prices.tariffs", SimpleNamespace(TARIFFS={"G12": 0, "G12w": 0})
    )
    return installed


def _hass_with_prices(*prices: FakeConfigEntry) -> Any:
    """A hass holding the given enea_prices entries."""
    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [e for e in prices if e.domain == domain]
        )
    )


async def test_an_installed_enea_prices_supports_the_groups_of_its_table(prices_installed) -> None:
    hass = _hass_with_prices()

    assert await issues_module.async_prices_supports(hass, "g12w")
    assert not await issues_module.async_prices_supports(hass, "G11pewna")
    assert not await issues_module.async_prices_supports(hass, None)


async def test_without_enea_prices_installed_nothing_is_supported(prices_installed) -> None:
    prices_installed.clear()

    assert not await issues_module.async_prices_supports(_hass_with_prices(), "G12")


async def test_setting_up_enea_prices_is_suggested_for_a_supported_group(
    registry, prices_installed
) -> None:
    await issues_module.async_update_prices_setup_issue(
        _hass_with_prices(), "entry1", "PPE", "G12"
    )

    issue = registry["open"]["prices_not_configured_entry1"]
    assert issue["is_fixable"]
    assert issue["translation_placeholders"] == {"meter_code": "PPE", "tariff": "G12"}


async def test_an_entry_of_the_group_clears_the_suggestion(registry, prices_installed) -> None:
    """Even one that failed to load: it is still the user's configuration."""
    await issues_module.async_update_prices_setup_issue(
        _hass_with_prices(), "entry1", "PPE", "G12"
    )
    entry = FakeConfigEntry(domain="enea_prices", data={"tariff": "G12"})

    await issues_module.async_update_prices_setup_issue(
        _hass_with_prices(entry), "entry1", "PPE", "G12"
    )

    assert registry["open"] == {}


async def test_nothing_is_suggested_without_enea_prices_or_for_another_group(
    registry, prices_installed
) -> None:
    await issues_module.async_update_prices_setup_issue(
        _hass_with_prices(), "entry1", "PPE", "G11pewna"
    )
    prices_installed.clear()
    await issues_module.async_update_prices_setup_issue(
        _hass_with_prices(), "entry1", "PPE", "G12"
    )

    assert registry["open"] == {}


async def test_the_setup_fix_continues_in_the_enea_prices_config_flow() -> None:
    started: list[tuple[str, dict[str, Any]]] = []

    async def async_init(domain: str, *, context: dict[str, Any]) -> dict[str, Any]:
        started.append((domain, context))
        return {"flow_id": "prices-flow"}

    flow = await repairs_module.async_create_fix_flow(None, "prices_not_configured_entry1", None)
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(
            flow=SimpleNamespace(
                async_init=async_init,
                async_get=lambda flow_id: {"context": {"source": "user"}},
            )
        )
    )
    flow.handler = "enea"
    flow.flow_id = "fix"

    result = await flow.async_step_confirm({})

    assert started == [("enea_prices", {"source": "user"})]
    assert result["type"] == "create_entry"
    assert result["next_flow"] == ("config_flow", "prices-flow")
