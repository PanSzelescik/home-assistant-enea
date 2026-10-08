"""The diagnostics report hides everything that identifies the customer."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.enea import diagnostics as diagnostics_module

from conftest import FakeHass

PPE = "590310600000000001"
METER_ID = 73689
SERIAL = "57354308"
AGREEMENT = "P/I/53/11111111/00001/0"


def _entry() -> Any:
    """Return a config entry stand-in whose coordinator holds a dashboard response."""

    async def async_refresh() -> None:
        """Refreshing is a no-op: the data below is what the portal returned."""

    dashboard = {
        "id": METER_ID,
        "name": PPE,
        "code": PPE,
        "agreementPower": 14,
        "tariffGroupName": "G12",
        "address": {"street": "ul. Testowa", "houseNum": "1", "city": "Poznań"},
        "meters": [
            {"id": 73688, "serialNumber": SERIAL, "typeName": "OTUS3", "disassemblyDate": None}
        ],
        "agreements": [{"id": 65520, "agreementNumber": AGREEMENT, "tariffGroupName": "G12"}],
        "billingWeekData": [{"zones": [{"id": 3466, "name": "Dzień"}]}],
    }
    coordinator = SimpleNamespace(
        async_refresh=async_refresh,
        last_update_success=True,
        update_interval=timedelta(hours=3),
        last_exception=None,
        data=dashboard,
        diagnostics_state=lambda: {"initial_backfill": "done"},
    )
    return SimpleNamespace(
        data={
            "username": "user@example.com",
            "password": "secret",
            "meter_id": METER_ID,
            "meter_name": PPE,
        },
        options={},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )


@pytest.fixture
async def report(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return the diagnostics report, with the statistics overview stubbed out."""

    async def overview(hass: Any, meter_code: str) -> dict[str, Any]:
        """Stand in for the recorder query; ids arrive already masked."""
        return {"enea:…0001_energia_pobrana": {"last_hour": "2026-10-07T23:00:00+02:00"}}

    monkeypatch.setattr(diagnostics_module, "async_statistics_overview", overview)
    return await diagnostics_module.async_get_config_entry_diagnostics(
        FakeHass(), _entry()  # type: ignore[arg-type]
    )


async def test_report_hides_identifying_data(report: dict[str, Any]) -> None:
    """No PPE, portal ids, serial, agreement number, address or credentials leak."""
    dumped = json.dumps(report, ensure_ascii=False)

    for secret in (
        PPE, str(METER_ID), "73688", "65520", SERIAL, AGREEMENT,
        "ul. Testowa", "user@example.com", "secret",
    ):
        assert secret not in dumped


async def test_report_keeps_what_diagnosis_needs(report: dict[str, Any]) -> None:
    """Zone names and ids, the meter model, the tariff and the state stay readable."""
    meter_data = report["meter_data"]

    assert meter_data["billingWeekData"][0]["zones"][0] == {"id": 3466, "name": "Dzień"}
    assert meter_data["meters"][0]["typeName"] == "OTUS3"
    assert meter_data["agreements"][0]["tariffGroupName"] == "G12"
    assert report["coordinator"]["initial_backfill"] == "done"
    assert report["phases"] == {
        "inferred": "three_phase", "source": "meter_model", "enea_prices": None
    }
    assert report["enea_prices"] is None
    assert "enea:…0001_energia_pobrana" in report["statistics"]


async def test_statistics_overview(monkeypatch: pytest.MonkeyPatch, wire_recorder) -> None:
    """Every series of the meter is listed with its newest hour, ids masked."""
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.enea import statistics as statistics_module

    newest = datetime(2026, 10, 7, 23, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    wire_recorder(statistics_module, [(newest, 1899.13)])
    own = f"enea:{PPE}_energia_pobrana"
    other = "enea:590310600000000002_energia_pobrana"
    monkeypatch.setattr(
        statistics_module,
        "get_metadata",
        lambda hass, statistic_source: {
            own: (1, {"name": "Energia pobrana", "unit_of_measurement": "kWh"}),
            other: (2, {"name": "Energia pobrana", "unit_of_measurement": "kWh"}),
        },
    )

    overview = await statistics_module.async_statistics_overview(object(), PPE)  # type: ignore[arg-type]

    assert overview == {
        "enea:…0001_energia_pobrana": {
            "name": "Energia pobrana",
            "unit": "kWh",
            "last_hour": "2026-10-07T23:00:00+02:00",
            "sum": 1899.13,
        }
    }


async def test_coordinator_state_reports_a_failed_backfill() -> None:
    """A backfill that failed in the background shows up with its masked error."""
    import asyncio

    from custom_components.enea.coordinator import EneaUpdateCoordinator

    coord = object.__new__(EneaUpdateCoordinator)
    coord._fetch_consumption = True
    coord._fetch_generation = False
    coord._fetch_power_consumption = False
    coord._fetch_power_generation = False
    coord._tariff_name = "G12"
    coord._assembly_datetime = None
    coord._cost_checked_until = None
    coord.statistics_until = None
    coord.bill_prev_reading = None
    coord.bill_last_reading = None
    coord.bill_estimates = {"bill_previous": None, "bill_current": None}
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    coord._backfill_task = done  # type: ignore[assignment]
    coord._backfill_error = "failed for enea:…0001_energia_pobrana"

    state = coord.diagnostics_state()

    assert state["initial_backfill"] == "failed: failed for enea:…0001_energia_pobrana"
    assert state["fetch_types"] == ["energy_consumed"]
