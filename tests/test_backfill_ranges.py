"""Historical fetches preserve calendar boundaries and real hourly slots."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import coordinator as module, statistics
from custom_components.enea.connector import EneaApiError
from custom_components.enea.const import MeasurementType, Resolution
from conftest import FakeHass


def _slots(day, value=1.0):
    """Build real elapsed hours between two Warsaw midnights, including DST."""
    start = datetime.combine(day, time(), dt_util.DEFAULT_TIME_ZONE).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), dt_util.DEFAULT_TIME_ZONE).astimezone(timezone.utc)
    return [
        {"timeId": index + 1, "integrationEnd": int((start + timedelta(hours=index + 1)).timestamp() * 1000),
         "items": [{"tarifZoneId": 1, "value": value}]}
        for index in range(int((end - start).total_seconds() / 3600))
    ]


@pytest.fixture
def coord(monkeypatch):
    """Keep range processing real while replacing network, clock and HA services."""
    result = object.__new__(module.EneaUpdateCoordinator)
    result._prosumer = False
    result._fetch_consumption = True
    result._fetch_generation = False
    result._fetch_power_consumption = False
    result._fetch_power_generation = False
    result._assembly_datetime = None
    result._zero_filled_days = set()
    result._meter_code = "590310600000001234"
    result._tariff_name = "G12"
    result._dashboard_data = {}
    result.meter_id = 12345
    result.client = SimpleNamespace(get_consumption_data_range=AsyncMock())
    result.hass = FakeHass()
    result.async_update_listeners = Mock()
    monkeypatch.setattr(module.dt_util, "now", lambda: datetime(2026, 11, 1, 12, tzinfo=dt_util.DEFAULT_TIME_ZONE))
    return result


@pytest.mark.parametrize("day,hours,local_hours", [
    pytest.param(date(2026, 3, 29), 23, [0, 1, *range(3, 24)], id="spring"),
    pytest.param(date(2026, 10, 25), 25, [0, 1, 2, 2, *range(3, 24)], id="autumn"),
])
async def test_dst_range_preserves_every_hour_through_statistics(coord, monkeypatch, day, hours, local_hours):
    """Adjacent days and repeated hours remain distinct through splitting and injection."""
    before, after = day - timedelta(days=1), day + timedelta(days=1)
    slots = _slots(before) + _slots(day) + _slots(after)
    coord.client.get_consumption_data_range.return_value = {"values": slots, "zones": []}
    writes = Mock()
    monkeypatch.setattr(statistics, "async_add_external_statistics", writes)
    monkeypatch.setattr(statistics, "_newest_entry", AsyncMock(return_value=(None, 0.0)))

    days = await coord._fetch_range(before, after)
    await statistics.async_insert_historical_statistics(coord.hass, coord._meter_code, days)

    assert [d for d, _ in days] == [before, day, after]
    assert [len(data["energy_consumed"]["values"]) for _, data in days] == [24, hours, 24]
    writes.assert_called_once()
    rows = writes.call_args.args[2]
    stamps = [row["start"].timestamp() for row in rows]
    assert stamps == [slot["integrationEnd"] / 1000 - 3600 for slot in slots]
    assert len(set(stamps)) == 48 + hours
    assert [row["start"].hour for row in rows[24:24 + hours]] == local_hours
    assert [row["sum"] for row in rows] == list(range(1, 49 + hours))
    assert [row["state"] for row in rows] == [1.0] * (48 + hours)


@pytest.mark.parametrize("primary,table", [
    pytest.param(1, 24, id="table-longer"),
    pytest.param(24, 1, id="primary-longer"),
    pytest.param(0, 24, id="only-table"),
])
async def test_range_uses_complete_response_and_keeps_zone_names(coord, primary, table):
    """A partial chart series never hides the complete table series or tariff zones."""
    day = date(2026, 6, 1)
    slots = _slots(day)
    zones = [{"id": 1, "name": "Strefa testowa"}]
    coord.client.get_consumption_data_range.return_value = {
        "values": slots[:primary], "valuesToTable": slots[:table], "zones": zones,
    }

    result = await coord._fetch_range(day, day)

    assert result == [(day, {"energy_consumed": {"values": slots, "zones": zones}})]


@pytest.mark.parametrize("day", [date(2026, 6, 1), date(2026, 3, 29), date(2026, 10, 25)],
                         ids=["ordinary", "spring", "autumn"])
async def test_assembly_clamps_range_and_keeps_hour_containing_installation(coord, day):
    """Pre-installation slots are removed using timestamps, not ordinal timeId."""
    coord._assembly_datetime = datetime.combine(day, time(12, 13), dt_util.DEFAULT_TIME_ZONE)
    next_day = day + timedelta(days=1)
    slots = _slots(day) + _slots(next_day)
    coord.client.get_consumption_data_range.return_value = {"values": slots}

    result = await coord._fetch_days_forward(day - timedelta(days=400), next_day)

    coord.client.get_consumption_data_range.assert_awaited_once_with(
        12345, day, next_day, MeasurementType.ENERGY_CONSUMED, Resolution.MIN_60, None,
    )
    cutoff = datetime.combine(day, time(12), dt_util.DEFAULT_TIME_ZONE).timestamp()
    assert [d for d, _ in result] == [day, next_day]
    assert [slot["integrationEnd"] / 1000 - 3600 for slot in result[0][1]["energy_consumed"]["values"]] == [
        cutoff + hour * 3600 for hour in range(12)
    ]
    assert result[1][1]["energy_consumed"]["values"] == _slots(next_day)


@pytest.mark.parametrize("length,offsets", [
    pytest.param(1, [(0, 0)], id="single-day"),
    pytest.param(180, [(0, 179)], id="exact-chunk"),
    pytest.param(181, [(0, 179), (180, 180)], id="one-over"),
    pytest.param(361, [(0, 179), (180, 359), (360, 360)], id="three-chunks"),
])
async def test_forward_chunks_cover_inclusive_range_once(coord, length, offsets):
    """Chunk boundaries neither skip nor duplicate a day and preserve zero-fill policy."""
    start = date(2025, 1, 1)

    async def fetch(first, last, zero_fill, grace, skip_leading):
        """Represent every fetched day without building hundreds of hourly responses."""
        return [(first + timedelta(days=i), {}) for i in range((last - first).days + 1)]

    coord._fetch_range = AsyncMock(side_effect=fetch)

    result = await coord._fetch_days_forward(start, start + timedelta(days=length - 1), True, 14)

    assert [call.args for call in coord._fetch_range.await_args_list] == [
        (start + timedelta(days=first), start + timedelta(days=last), True, 14, False)
        for first, last in offsets
    ]
    assert [day for day, _ in result] == [start + timedelta(days=i) for i in range(length)]


@pytest.mark.parametrize("reason", ["reversed", "before-assembly", "disabled"])
async def test_empty_fetch_ranges_never_contact_api(coord, reason):
    """Impossible dates and disabled measurements must not generate HTTP requests."""
    start, end = date(2026, 6, 1), date(2026, 6, 2)
    if reason == "reversed":
        start, end = end, start
    elif reason == "before-assembly":
        coord._assembly_datetime = datetime(2026, 6, 3, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    else:
        coord._fetch_consumption = False

    assert await coord._fetch_days_forward(start, end) == []
    coord.client.get_consumption_data_range.assert_not_awaited()


@pytest.mark.parametrize("age,zero_fill,expected", [
    pytest.param(2, True, False, id="before-grace"),
    pytest.param(3, True, True, id="at-grace"),
    pytest.param(4, True, True, id="after-grace"),
    pytest.param(30, False, False, id="backward-scan-keeps-gaps"),
])
async def test_missing_day_respects_grace_boundary(coord, age, zero_fill, expected):
    """Only sufficiently old null slots become zeroes; the API response is unchanged."""
    day = date(2026, 11, 1) - timedelta(days=age)
    slots = _slots(day, None)
    coord.client.get_consumption_data_range.return_value = {"values": slots}

    result = await coord._fetch_range(day, day, zero_fill, 3)

    assert [d for d, _ in result] == ([day] if expected else [])
    assert coord._zero_filled_days == ({day} if expected else set())
    if expected:
        assert result[0][1]["energy_consumed"]["values"] == _slots(day, 0.0)
    assert slots == _slots(day, None)


async def test_missing_slots_are_not_fabricated_and_zero_consumption_is_data(coord):
    """A blank response stays blank; an explicitly zero-valued recent day is imported."""
    day = date(2026, 10, 31)
    coord.client.get_consumption_data_range.side_effect = [{"values": []}, {"values": _slots(day, 0.0)}]

    assert await coord._fetch_range(day, day, True, 0) == []
    result = await coord._fetch_range(day, day, True, 3)

    assert result == [(day, {"energy_consumed": {"values": _slots(day, 0.0), "zones": []}})]
    assert coord._zero_filled_days == set()


@pytest.mark.parametrize("empty_prefix,calls", [(6, 2), (7, 1), (8, 1)],
                         ids=["below-limit", "at-limit", "above-limit"])
async def test_backward_scan_stops_at_seven_empty_days(coord, empty_prefix, calls):
    """Six missing oldest days still allow older history; seven establish its boundary."""
    end = date(2026, 6, 30)
    first = end - timedelta(days=179)
    data = [(first + timedelta(days=empty_prefix), {"marker": "oldest"}), (end, {"marker": "latest"})]
    coord._fetch_range = AsyncMock(side_effect=[data, []])

    result = await coord._fetch_days_backward(end)

    assert result == data
    assert coord._fetch_range.await_count == calls
    assert coord._fetch_range.await_args_list[0].args == (first, end)
    assert coord._fetch_range.await_args_list[0].kwargs == {}
    if calls == 2:
        assert coord._fetch_range.await_args_list[1].args == (first - timedelta(days=180), first - timedelta(days=1))


async def test_backward_chunks_are_returned_oldest_first(coord):
    """Fetching newest chunks first must not reverse the injected chronology."""
    end = date(2026, 6, 30)
    first = end - timedelta(days=179)
    newer = [(first, {"value": 2}), (end, {"value": 3})]
    older = [(first - timedelta(days=170), {"value": 1})]
    coord._fetch_range = AsyncMock(side_effect=[newer, older])

    assert await coord._fetch_days_backward(end) == older + newer
    assert coord._fetch_range.await_count == 2


async def test_known_assembly_backfills_forward_with_zero_fill(coord):
    """Known installation bounds avoid scanning and zero-fill a prosumer's stale days."""
    coord._assembly_datetime = datetime(2026, 10, 1, 12, 13, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    coord._prosumer = True
    days = [(date(2026, 10, 2), {"energy_consumed": {"values": _slots(date(2026, 10, 2))}})]
    coord._fetch_days_forward = AsyncMock(return_value=days)

    assert await coord._fetch_days_backward(date(2026, 10, 31)) == days
    coord._fetch_days_forward.assert_awaited_once_with(
        date(2026, 10, 1), date(2026, 10, 31), zero_fill_stale=True, grace_days=3,
        skip_leading_gaps=True,
    )


async def test_days_before_the_portals_history_are_skipped_not_zeroed(coord):
    """A prosumer's balanced data starts long after the meter's assembly.

    The backfill zero-filled every day from the assembly up to the first one
    with data, which showed as months of zero consumption.  A gap after the
    history has begun is still zero-filled.
    """
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(4)]
    values = [_slots(days[0], None), _slots(days[1], 2.0), _slots(days[2], None), _slots(days[3], 1.0)]
    coord.client.get_consumption_data_range.return_value = {
        "values": [slot for day_slots in values for slot in day_slots]
    }

    result = await coord._fetch_range(days[0], days[3], True, 3, skip_leading_gaps=True)

    assert [d for d, _ in result] == days[1:]
    assert coord._zero_filled_days == {days[2]}


async def test_zero_filled_days_are_logged_once_per_fetch(coord, caplog):
    """A long gap logs one summary line, not a line per day."""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    coord.client.get_consumption_data_range.return_value = {
        "values": [slot for day in days for slot in _slots(day, None)]
    }
    caplog.set_level(logging.INFO, logger=module.__name__)

    result = await coord._fetch_range(days[0], days[-1], True, 3)

    assert len(result) == 30
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "30 day(s) from 2026-09-01 to 2026-09-30" in message
    assert "590310600000001234" not in message


async def test_leading_gaps_are_skipped_only_until_the_history_begins(coord):
    """Chunks after the first one with data zero-fill their gaps again."""
    start = date(2025, 1, 1)
    coord._fetch_range = AsyncMock(side_effect=[[], [(start + timedelta(days=200), {})], []])

    await coord._fetch_days_forward(start, start + timedelta(days=539), True, 3, skip_leading_gaps=True)

    assert [call.args[4] for call in coord._fetch_range.await_args_list] == [True, True, False]


async def test_empty_manual_backfill_does_not_write_or_notify(coord):
    """An entirely empty response yields zero imported days and no recorder writes."""
    coord.client.get_consumption_data_range.return_value = {"values": []}
    coord._async_inject_days = AsyncMock()

    assert await coord.async_backfill(date(2026, 6, 1), date(2026, 6, 2)) == 0
    coord._async_inject_days.assert_not_awaited()
    coord.async_update_listeners.assert_not_called()


async def test_manual_backfill_refetches_zeroes_then_corrects_stored_history(coord, wire_recorder):
    """Real range parsing and injection repair a missing day and remain idempotent."""
    day = date(2026, 6, 1)
    slot = _slots(day)[0]
    hour = datetime.fromtimestamp(slot["integrationEnd"] / 1000 - 3600, timezone.utc)
    store = wire_recorder(statistics, [
        (hour - timedelta(hours=1), 100.0, 2.0),
        (hour, 100.0, 0.0),
        (hour + timedelta(days=1), 105.0, 5.0),
    ])
    coord.client.get_consumption_data_range.side_effect = [
        {"values": [{**slot, "items": [{"value": value}]}]} for value in (None, 4.0, 4.0)
    ]

    assert await coord.async_backfill(day, day) == 1
    assert store.totals == [100.0, 100.0, 105.0]
    assert await coord.async_backfill(day, day) == 1
    assert store.totals == [100.0, 104.0, 109.0]
    assert await coord.async_backfill(day, day) == 1
    assert store.totals == [100.0, 104.0, 109.0]
    assert [row[2] for row in store.recorder.stored] == [2.0, 4.0, 5.0]
    assert coord.client.get_consumption_data_range.await_count == 3
    assert coord.async_update_listeners.call_count == 3


async def test_range_failure_keeps_other_measurements_but_total_failure_propagates(coord):
    """One failed measurement does not discard another; a wholly failed fetch fails."""
    day = date(2026, 6, 1)
    coord._fetch_generation = True
    error = EneaApiError("offline")
    coord.client.get_consumption_data_range.side_effect = [error, {"values": _slots(day, 2.0)}]

    result = await coord._fetch_range(day, day)
    assert result == [(day, {"energy_returned": {"values": _slots(day, 2.0), "zones": []}})]

    coord.client.get_consumption_data_range.side_effect = error
    with pytest.raises(EneaApiError, match="offline"):
        await coord.async_backfill(day, day)
    coord.async_update_listeners.assert_not_called()


async def test_range_cancellation_propagates(coord):
    """Cancellation must not become a missing day or a partial successful import."""
    coord.client.get_consumption_data_range.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await coord.async_backfill(date(2026, 6, 1), date(2026, 6, 1))
    coord.async_update_listeners.assert_not_called()


async def test_manual_backfill_writes_the_costs_anew(coord):
    """The action corrects old costs, so an hour stored in another zone is cleared."""
    days = [(date(2026, 10, 2), {})]
    coord._fetch_days_forward = AsyncMock(return_value=days)
    coord._async_inject_days = AsyncMock()

    await coord.async_backfill(date(2026, 10, 2), date(2026, 10, 2))

    coord._async_inject_days.assert_awaited_once_with(days, rewrite=True)
