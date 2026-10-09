"""The diagnostics report hides everything that identifies the customer."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import diagnostics as diagnostics_module
from custom_components.enea.installation import DetectedInstallation

from conftest import FakeConfigEntry, FakeHass

PPE = "590310600000000001"
METER_ID = 12345
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
        "billingWeekData": [
            {"measurementId": 1, "zones": [{"id": 3466, "name": "Dzień"}]},
            {"measurementId": 3, "measurementName": "Energia bierna kwadrant I", "zones": []},
        ],
    }
    coordinator = SimpleNamespace(
        async_refresh=async_refresh,
        last_update_success=True,
        update_interval=timedelta(hours=3),
        last_exception=None,
        data=dashboard,
        diagnostics_state=lambda: {"initial_backfill": "done", "statistics_until": "2026-10-07"},
        detected_installation=DetectedInstallation(
            phases=3,
            phases_source="meter_model",
            annual_kwh=3170.04,
            annual_kwh_source="billing_periods",
            annual_kwh_until=date(2026, 8, 5),
        ),
    )
    return SimpleNamespace(
        entry_id="entry1",
        data={
            "username": "user@example.com",
            "password": "secret",
            "meter_id": METER_ID,
            "meter_name": PPE,
        },
        options={},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )


def _hass() -> Any:
    """Return a hass with an enea_prices entry and the entities' current states."""
    prices = FakeConfigEntry(
        domain="enea_prices",
        data={"tariff": "G12"},
        runtime_data=SimpleNamespace(
            tariff=SimpleNamespace(
                name="G12",
                periods=[
                    SimpleNamespace(valid_from=date(2026, 1, 1), valid_until=date(2026, 6, 30)),
                    SimpleNamespace(valid_from=date(2026, 7, 1), valid_until=date(2026, 12, 31)),
                ],
            ),
            phases=1,
            annual_kwh=5000,
            billing_months=2,
        ),
    )
    hass = FakeHass([prices])
    changed = datetime(2026, 10, 9, 13, 0)
    states = {
        f"sensor.enea_{PPE}_grupa_taryfowa": SimpleNamespace(
            state="G12", last_changed=changed,
            attributes={"friendly_name": f"Enea {PPE} Grupa taryfowa", "zones": ["Dzień 1.8.1"]},
        ),
        f"sensor.enea_{PPE}_adres": SimpleNamespace(
            state="ul. Testowa 1, Poznań", last_changed=changed,
            attributes={"friendly_name": f"Enea {PPE} Adres", "street": "ul. Testowa"},
        ),
        f"sensor.enea_{PPE}_statystyki_aktualne_do": SimpleNamespace(
            state="unknown", last_changed=changed, attributes={},
        ),
    }
    hass.states = SimpleNamespace(get=states.get)  # pyright: ignore[reportAttributeAccessIssue]
    return hass


def _registry_entries() -> list[Any]:
    """Return the entity registry entries of the config entry."""
    return [
        SimpleNamespace(
            unique_id=f"enea-{PPE}-{key}", entity_id=entity_id, domain="sensor", disabled_by=None
        )
        for key, entity_id in (
            ("tariff", f"sensor.enea_{PPE}_grupa_taryfowa"),
            ("address", f"sensor.enea_{PPE}_adres"),
            ("statistics_until", f"sensor.enea_{PPE}_statystyki_aktualne_do"),
        )
    ]


@pytest.fixture
async def report(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return the diagnostics report, with the recorder and the registry stubbed out."""

    async def overview(hass: Any, meter_code: str) -> dict[str, Any]:
        """Stand in for the recorder query; ids arrive already masked."""
        return {"enea:…0001_energia_pobrana": {"last_hour": "2026-10-07T23:00:00+02:00"}}

    monkeypatch.setattr(diagnostics_module, "async_statistics_overview", overview)
    monkeypatch.setattr(diagnostics_module.er, "async_get", lambda hass: None)
    monkeypatch.setattr(
        diagnostics_module.er,
        "async_entries_for_config_entry",
        lambda registry, entry_id: _registry_entries() if entry_id == "entry1" else [],
    )
    return await diagnostics_module.async_get_config_entry_diagnostics(_hass(), _entry())


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
    assert report["installation"] == {
        "detected": {
            "phases": 3,
            "phases_source": "meter_model",
            "billing_months": None,
            "billing_months_source": None,
            "annual_kwh": 3170.0,
            "annual_kwh_source": "billing_periods",
            "annual_kwh_until": "2026-08-05",
            "annual_kwh_partial": False,
            "capacity_bracket": 3,
        },
        "enea_prices": {"phases": 1, "billing_months": 2, "annual_kwh": 5000},
    }
    assert "enea:…0001_energia_pobrana" in report["statistics"]


async def test_report_drops_reactive_energy(report: dict[str, Any]) -> None:
    """Only active energy is kept from billingWeekData."""
    ids = [m["measurementId"] for m in report["meter_data"]["billingWeekData"]]

    assert ids == [1]


async def test_report_shows_entity_states(report: dict[str, Any]) -> None:
    """Each entity's state is listed by key, so it can be held against the coordinator."""
    entities = report["entities"]

    assert set(entities) == {"tariff", "address", "statistics_until"}
    assert entities["tariff"]["state"] == "G12"
    assert entities["tariff"]["attributes"] == {"zones": ["Dzień 1.8.1"]}
    assert entities["statistics_until"]["state"] == "unknown"
    assert entities["address"]["state"] == "**REDACTED**"
    assert entities["address"]["attributes"] == "**REDACTED**"


async def test_report_shows_prices_coverage(report: dict[str, Any]) -> None:
    """The last day the enea_prices table can price is visible."""
    prices = report["enea_prices"]

    assert prices["covered_until"] == "2026-12-31"
    assert prices["periods"] == [["2026-01-01", "2026-06-30"], ["2026-07-01", "2026-12-31"]]


async def test_statistics_overview(monkeypatch: pytest.MonkeyPatch, wire_recorder) -> None:
    """Every series of the meter is listed with its newest hour, ids masked."""
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


async def test_coordinator_state_reports_failures() -> None:
    """Background failures and zero-filled days show up in the report."""
    import asyncio

    from custom_components.enea.coordinator import EneaUpdateCoordinator

    coord = object.__new__(EneaUpdateCoordinator)
    coord._fetch_consumption = True
    coord._fetch_generation = False
    coord._fetch_power_consumption = False
    coord._fetch_power_generation = False
    coord._prosumer = False
    coord._tariff_name = "G12"
    coord.hass = FakeHass()
    coord._dashboard_data = {
        "agreements": [
            {"from": _local_ms(date(2025, 12, 13)), "to": None, "tariffGroupName": "G12"},
            {
                "from": _local_ms(date(2022, 1, 8)),
                "to": _local_ms(date(2025, 12, 13)),
                "tariffGroupName": "G11",
            },
        ]
    }
    coord._assembly_datetime = None
    coord._cost_checked_until = None
    coord._costs_repriced_from = None
    coord.statistics_until = None
    coord.bill_prev_reading = None
    coord.bill_last_reading = None
    coord.bill_estimates = {"bill_previous": None, "bill_current": None}
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    coord._backfill_task = done  # type: ignore[assignment]
    coord._backfill_error = "failed for enea:…0001_energia_pobrana"
    coord._statistics_last_run = datetime(2026, 10, 9, 13, 0)
    coord._statistics_error = "EneaApiError: Unexpected response from range endpoint: 500"
    coord._zero_filled_days = {date(2026, 10, 3), date(2026, 10, 1)}

    state = coord.diagnostics_state()

    assert state["initial_backfill"] == "failed: failed for enea:…0001_energia_pobrana"
    assert state["fetch_types"] == ["energy_consumed"]
    assert state["statistics_last_run"] == "2026-10-09T13:00:00"
    assert state["statistics_error"] == "EneaApiError: Unexpected response from range endpoint: 500"
    assert state["zero_filled_days"] == ["2026-10-01", "2026-10-03"]
    assert state["tariff_groups"] == [
        {"from": "2022-01-08", "until": "2025-12-13", "group": "G11", "priced": False},
        {"from": "2025-12-13", "until": None, "group": "G12", "priced": False},
    ]


def _local_ms(day: date) -> int:
    """Local midnight of a day as the portal's millisecond timestamp."""
    return int(dt_util.start_of_local_day(day).timestamp() * 1000)
