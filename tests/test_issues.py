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
from custom_components.enea.coordinator import EneaUpdateCoordinator


@dataclass
class _Prices:
    """The only part of PricesConfig the issues read."""

    phases: int


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """Record issue registry calls and let a test choose the enea_prices phases."""
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


def _hass(language: str = "pl") -> Any:
    """Return a hass stand-in exposing only the configured language."""
    return SimpleNamespace(config=SimpleNamespace(language=language))


def _update(data: dict[str, Any], language: str = "pl") -> None:
    """Run one issue update for config entry "entry1" (PPE number "PPE")."""
    issues_module.async_update_issues(_hass(language), "entry1", "PPE", "G12", data)


def test_known_model_matching_prices_raises_nothing(registry) -> None:
    """A known meter that agrees with enea_prices leaves Repairs empty."""
    registry["prices"] = _Prices(phases=3)

    _update(_dashboard("OTUS3"))

    assert registry["open"] == {}


def test_phase_mismatch_is_raised_and_cleared(registry) -> None:
    """A wrong phase count in enea_prices is reported until it is fixed."""
    registry["prices"] = _Prices(phases=1)
    _update(_dashboard("OTUS3"))

    issue = registry["open"]["phases_mismatch_entry1"]
    assert issue["translation_placeholders"]["detected"] == "3"
    assert issue["translation_placeholders"]["configured"] == "1"

    registry["prices"] = _Prices(phases=3)
    _update(_dashboard("OTUS3"))

    assert "phases_mismatch_entry1" not in registry["open"]


def test_no_mismatch_without_prices_or_phases(registry) -> None:
    """Nothing to compare with — no enea_prices, or phases unknown — no issue."""
    _update(_dashboard("OTUS3"))
    registry["prices"] = _Prices(phases=1)
    _update(_dashboard("XYZ123", capacity=7))

    assert "phases_mismatch_entry1" not in registry["open"]


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
    registry["prices"] = _Prices(phases=1)
    _update(_dashboard("XYZ123"))
    assert registry["open"]

    issues_module.async_delete_issues(_hass(), "entry1")

    assert registry["open"] == {}


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
