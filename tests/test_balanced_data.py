"""A prosumer's energy comes from the balanced data the invoice is settled from."""
from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea.connector import EneaApiClient
from custom_components.enea.const import (
    CONF_BALANCED_HISTORY,
    DataSource,
    MeasurementType,
    Resolution,
)
from custom_components.enea.coordinator import EneaUpdateCoordinator
from custom_components.enea.statistics import get_statistic_id

METER_CODE = "590310600000001234"


async def _requested_path(data_source: DataSource | None) -> str:
    """Return the API path one range request asks for."""
    client = object.__new__(EneaApiClient)
    asked: list[str] = []

    async def request(url: str, label: str) -> dict[str, Any]:
        asked.append(url)
        return {}

    client._request = request  # type: ignore[method-assign]
    await client.get_consumption_data_range(
        12345,
        datetime.date(2026, 10, 1),
        datetime.date(2026, 10, 7),
        MeasurementType.ENERGY_CONSUMED,
        Resolution.MIN_60,
        data_source,
    )
    return asked[0].split("/api", 1)[1]


async def test_the_data_source_is_one_more_path_segment() -> None:
    """Portal Odbiorcy Enea joins the query's values with "/", data source last."""
    assert await _requested_path(None) == "/consumption/12345/2026-10-01/2026-10-07/1/2"
    assert (
        await _requested_path(DataSource.AFTER_BALANCING)
        == "/consumption/12345/2026-10-01/2026-10-07/1/2/2"
    )


def _coordinator(
    prosumer: bool,
    power: bool = False,
    entry_data: dict[str, Any] | None = None,
) -> EneaUpdateCoordinator:
    """Return a coordinator built without __init__, which needs a running Home Assistant."""
    coord = object.__new__(EneaUpdateCoordinator)
    coord._prosumer = prosumer
    coord._fetch_consumption = True
    coord._fetch_generation = True
    coord._fetch_power_consumption = power
    coord._fetch_power_generation = False
    coord._meter_code = METER_CODE
    coord.meter_id = 12345
    coord._backfill_task = None
    coord._assembly_datetime = None
    coord._zero_filled_days = set()
    coord.async_update_listeners = lambda: None  # type: ignore[method-assign]

    def update_entry(entry: Any, data: dict[str, Any]) -> None:
        entry.data = data

    coord.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_update_entry=update_entry),
        async_create_task=lambda coro, name=None: asyncio.ensure_future(coro),
    )
    coord.config_entry = SimpleNamespace(data=dict(entry_data or {}))  # type: ignore[assignment]
    return coord


def test_only_a_prosumers_energy_comes_after_balancing() -> None:
    """The portal has balanced data for energy only, and only for prosumers."""
    prosumer = _coordinator(prosumer=True)
    assert prosumer._data_source(MeasurementType.ENERGY_CONSUMED) is DataSource.AFTER_BALANCING
    assert prosumer._data_source(MeasurementType.ENERGY_RETURNED) is DataSource.AFTER_BALANCING
    assert prosumer._data_source(MeasurementType.POWER_CONSUMED) is None
    assert prosumer._data_source(MeasurementType.POWER_RETURNED) is None

    consumer = _coordinator(prosumer=False)
    assert consumer._data_source(MeasurementType.ENERGY_CONSUMED) is None


async def test_the_range_fetch_asks_for_balanced_energy() -> None:
    """Every energy request of a prosumer carries the data source, power none."""
    coord = _coordinator(prosumer=True, power=True)
    asked: dict[MeasurementType, DataSource | None] = {}

    async def get_range(meter_id, start, end, mtype, resolution, data_source=None):
        asked[mtype] = data_source
        return {"values": [], "zones": []}

    coord.client = SimpleNamespace(get_consumption_data_range=get_range)  # type: ignore[assignment]
    day = datetime.date(2026, 10, 1)

    await coord._fetch_range(day, day)

    assert asked == {
        MeasurementType.ENERGY_CONSUMED: DataSource.AFTER_BALANCING,
        MeasurementType.ENERGY_RETURNED: DataSource.AFTER_BALANCING,
        MeasurementType.POWER_CONSUMED: None,
    }


def _hour(day: datetime.date) -> datetime.datetime:
    """The last hour of a local day."""
    return datetime.datetime.combine(day, datetime.time(23), tzinfo=dt_util.DEFAULT_TIME_ZONE)


def _wire_statistics(
    monkeypatch: pytest.MonkeyPatch, newest: dict[str, datetime.date]
) -> None:
    """Answer get_last_statistics with the newest day of each named series."""
    ids = {
        get_statistic_id(METER_CODE, name): day for name, day in newest.items()
    }

    def last_statistics(hass, count, sid, convert, types):
        if sid not in ids:
            return {}
        return {sid: [{"start": _hour(ids[sid]).timestamp(), "sum": 1.0}]}

    async def run(target, *args):
        return target(*args)

    async def drained() -> None:
        """Writes in these tests are stubbed, nothing is queued."""

    recorder = SimpleNamespace(async_add_executor_job=run, async_block_till_done=drained)
    monkeypatch.setattr(coordinator_module, "get_instance", lambda hass: recorder)
    monkeypatch.setattr(coordinator_module, "get_last_statistics", last_statistics)


def _record_fetches(coord: EneaUpdateCoordinator) -> dict[str, list[Any]]:
    """Stub the portal and the writes; record what was fetched and how."""
    calls: dict[str, list[Any]] = {"forward": [], "backward": [], "injected": []}

    async def forward(start, end, **kwargs):
        calls["forward"].append((start, end))
        return []

    async def backward(end):
        calls["backward"].append(end)
        return [(end, {})]

    async def inject(days):
        calls["injected"].append(days)

    async def costs(up_to):
        """Costs are covered by their own tests."""

    coord._fetch_days_forward = forward  # type: ignore[method-assign]
    coord._fetch_days_backward = backward  # type: ignore[method-assign]
    coord._async_inject_days = inject  # type: ignore[method-assign]
    coord._async_inject_missing_costs = costs  # type: ignore[method-assign]
    return calls


def _days_ago(days: int) -> datetime.date:
    """A local day counted back from today."""
    return dt_util.now().date() - datetime.timedelta(days=days)


async def test_a_prosumers_old_history_is_reimported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History imported before balanced data was used is replaced, then left alone.

    Without the re-import the days before the update would stay at the data
    before balancing while the later ones are balanced, and the costs and the
    bill would mix the two.
    """
    _wire_statistics(monkeypatch, {"Energia pobrana": _days_ago(3)})
    coord = _coordinator(prosumer=True)
    calls = _record_fetches(coord)

    await coord._async_fetch_and_inject_stats()
    await coord._backfill_task

    assert calls["backward"] == [_days_ago(1)]
    assert calls["forward"] == [], "no incremental update on top of the re-import"
    assert coord.config_entry.data[CONF_BALANCED_HISTORY] is True

    await coord._async_fetch_and_inject_stats()

    assert calls["backward"] == [_days_ago(1)], "re-imported only once"
    assert calls["forward"] == [(_days_ago(2), _days_ago(1))]


async def test_a_consumer_is_not_reimported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A meter without balanced data keeps its history and its entry untouched."""
    _wire_statistics(monkeypatch, {"Energia pobrana": _days_ago(3)})
    coord = _coordinator(prosumer=False)
    calls = _record_fetches(coord)

    await coord._async_fetch_and_inject_stats()

    assert coord._backfill_task is None
    assert calls["forward"] == [(_days_ago(2), _days_ago(1))]
    assert CONF_BALANCED_HISTORY not in coord.config_entry.data


async def test_a_prosumers_new_history_is_marked_balanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first backfill of a new prosumer already is balanced — no second one."""
    _wire_statistics(monkeypatch, {})
    coord = _coordinator(prosumer=True)
    calls = _record_fetches(coord)

    await coord._async_fetch_and_inject_stats()
    await coord._backfill_task

    assert calls["backward"] == [_days_ago(1)]
    assert coord.config_entry.data[CONF_BALANCED_HISTORY] is True


async def test_power_ahead_of_balanced_energy_does_not_hide_missing_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incremental update starts after the newest balanced energy, not power.

    A prosumer's power still comes from the data before balancing.  Were the
    balanced energy published later, the power series reaching yesterday would
    make the energy look up to date and its newest days would never be fetched.
    """
    _wire_statistics(
        monkeypatch,
        {"Energia pobrana": _days_ago(5), "Energia oddana": _days_ago(5), "Moc pobrana": _days_ago(1)},
    )
    coord = _coordinator(prosumer=True, power=True, entry_data={CONF_BALANCED_HISTORY: True})
    calls = _record_fetches(coord)

    await coord._async_fetch_and_inject_stats()

    assert calls["forward"] == [(_days_ago(4), _days_ago(1))]
