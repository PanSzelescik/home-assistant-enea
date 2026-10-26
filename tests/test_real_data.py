"""Saved responses of a real account go through parsing, statistics and sensors.

The fixtures in tests/fixtures/portal come from one G12 household whose OTUS3
meter was assembled on 2026-03-20 at 12:12 local time, replacing an MT174 that
sent no hourly data.  Identifiers are made up and every energy and power value
is scaled (scripts/anonymize_fixtures.py), so the tests compare the data with
itself — with the portal's own sums in the same response — rather than with
numbers written down here.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util

from conftest import PORTAL_FIXTURES, FakeHass, load_portal_fixture
from custom_components.enea import config_flow, costs, statistics
from custom_components.enea import coordinator as coordinator_module
from custom_components.enea.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.enea.connector import (
    agreement_tariffs,
    billing_period_starts,
    get_active_meter,
    infer_phases,
    zone_name,
)
from custom_components.enea.const import (
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    STAT_KEY_POWER_CONSUMED,
    STAT_KEY_POWER_RETURNED,
    MeasurementType,
)
from custom_components.enea.diagnostics import _redact_meter_data
from custom_components.enea.installation import detect_billing_months, latest_reading_day
from custom_components.enea.parsing import assemble_days, has_data, slot_start_dt, split_range_response
from custom_components.enea.sensor import SENSOR_DESCRIPTIONS

FAKE_PPE = "590310600000001234"
FAKE_METER_ID = 12345

LONG_RANGE = "range_2026-03-01_2026-10-10_1"
"""Energy consumed from before the assembly until the day the response was saved."""

SPRING_WEEK = {
    STAT_KEY_ENERGY_CONSUMED: "range_2026-03-27_2026-04-02_1",
    STAT_KEY_POWER_CONSUMED: "range_2026-03-27_2026-04-02_4",
    STAT_KEY_ENERGY_RETURNED: "range_2026-03-27_2026-04-02_5",
    STAT_KEY_POWER_RETURNED: "range_2026-03-27_2026-04-02_9",
}
"""Every measurement type over the week clocks went forward (2026-03-29)."""

SAVED_ON = date(2026, 10, 10)
"""The day the responses were saved; the portal had nothing for it yet."""

SPRING_FORWARD = date(2026, 3, 29)

FALL_BACK = date(2026, 10, 25)

FALL_BACK_WEEK = "range_2026-10-19_2026-10-26_1"
"""Energy consumed over the week clocks went back, saved the day after."""

FALL_BACK_DAY = "range_2026-10-25_2026-10-25_1"
"""Energy consumed on the fall-back day alone, as the daily top-up asks for it."""

FALL_BACK_LONG = "range_2026-09-16_2026-10-26_1"
"""Six weeks around the fall-back day: 'values' holds daily sums, not hours."""

FALL_BACK_RANGES = [FALL_BACK_WEEK, FALL_BACK_DAY, FALL_BACK_LONG]

FALL_BACK_SAVED_ON = date(2026, 10, 26)


def _assembly() -> datetime:
    """Return the active meter's assembly the way the coordinator derives it."""
    meter = get_active_meter(load_portal_fixture("dashboard"))
    assert meter is not None
    return dt_util.utc_from_timestamp(meter["assemblyDate"] / 1000).astimezone(
        dt_util.DEFAULT_TIME_ZONE
    )


def _hours(day_data: dict) -> list[datetime]:
    """Return the start of every slot of one day's response."""
    return [slot_start_dt(entry) for entry in day_data["values"]]


# --- the range response -------------------------------------------------------


def test_long_range_is_split_into_hours_not_monthly_sums():
    """A range over several months carries monthly sums in 'values'.

    Their integrationEnd is not the end of the month they sum (March ends on
    31 May), so taken for hours they would land as single huge hours months
    away.  The hours in 'valuesToTable' are what gets split.
    """
    response = load_portal_fixture(LONG_RANGE)
    assert len(response["values"]) == 7
    assert {item["tarifZoneId"] for entry in response["values"] for item in entry["items"]} >= {-1}

    days = split_range_response(response)

    first, last = date(2026, 3, 1), SAVED_ON
    assert sorted(days) == [first + timedelta(days=i) for i in range((last - first).days + 1)]
    for day, day_data in days.items():
        hours = _hours(day_data)
        assert len(hours) == (23 if day == SPRING_FORWARD else 24), day
        assert all(hour.date() == day and hour.minute == 0 and hour.second == 0 for hour in hours)
        assert all(item["tarifZoneId"] != -1 for entry in day_data["values"] for item in entry["items"])


def test_hours_add_up_to_the_portals_own_monthly_sums():
    """Every month and zone the import produces matches the portal's monthly sum.

    The current month has no monthly sum yet, and the zone totals of the
    response leave it out as well: they are the sums of the complete months.
    """
    response = load_portal_fixture(LONG_RANGE)
    zone_names = {zone["id"]: zone["name"] for zone in response["zones"]}
    days, _ = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: response}, today=SAVED_ON, assembly=_assembly()
    )
    series: dict[str, list[tuple[datetime, float]]] = {}
    for _, day_data in days:
        statistics._collect_series(day_data[STAT_KEY_ENERGY_CONSUMED], "pobrana", "Energia", series)
    assert set(series) == {"Energia pobrana"} | {
        f"Energia pobrana – {name}" for name in zone_names.values()
    }

    by_month: dict[tuple[int, str], float] = defaultdict(float)
    for name, rows in series.items():
        for hour, value in rows:
            by_month[hour.month, name] += value

    for entry in response["values"]:
        month = entry["timeId"]
        for item in entry["items"]:
            name = (
                "Energia pobrana" if item["tarifZoneId"] == -1
                else f"Energia pobrana – {zone_names[item['tarifZoneId']]}"
            )
            assert by_month[month, name] == pytest.approx(item["value"], abs=1e-3), (month, name)

    summed_months = {entry["timeId"] for entry in response["values"]}
    assert SAVED_ON.month not in summed_months
    for zone in response["zones"]:
        name = f"Energia pobrana – {zone['name']}"
        complete = sum(by_month[month, name] for month in summed_months)
        assert complete == pytest.approx(zone["consumption"], abs=1e-3)
        assert by_month[SAVED_ON.month, name] > 0
    assert sum(v for _, v in series["Energia pobrana"]) == pytest.approx(
        sum(sum(v for _, v in series[f"Energia pobrana – {name}"]) for name in zone_names.values())
    )


def test_assembly_day_keeps_the_hour_of_the_installation_onwards():
    """The day before the assembly reads zero and the assembly day starts at its hour.

    The portal reports the day before as zero consumption (status 1), which
    has_data takes for data, and the assembly day's hours up to the assembly
    with status 2.  Only the hours from the installation hour on are the new
    meter's.
    """
    response = load_portal_fixture(LONG_RANGE)
    assembly = _assembly()
    assert (assembly.date(), assembly.hour, assembly.minute) == (date(2026, 3, 20), 12, 12)
    before = split_range_response(response)[date(2026, 3, 19)]
    assert has_data(before)
    assert all(item["value"] in (None, 0.0) for entry in before["values"] for item in entry["items"])

    days, zero_filled = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: response}, today=SAVED_ON, assembly=assembly
    )

    assert zero_filled == []
    assert days[0][0] == assembly.date()
    first_day = days[0][1][STAT_KEY_ENERGY_CONSUMED]
    assert [hour.hour for hour in _hours(first_day)] == list(range(12, 24))
    first_items = first_day["values"][0]["items"]
    assert [item["status"] for item in first_items if item["value"] is not None] == [2]
    assert sum(item["value"] or 0.0 for item in first_items) > 0


@pytest.mark.parametrize("today,last_day,zero_filled", [
    pytest.param(SAVED_ON, SAVED_ON - timedelta(days=1), [], id="not-published-yet"),
    pytest.param(SAVED_ON + timedelta(days=2), SAVED_ON - timedelta(days=1), [], id="within-grace"),
    pytest.param(SAVED_ON + timedelta(days=3), SAVED_ON, [SAVED_ON], id="given-up"),
])
def test_day_without_data_waits_out_the_grace_period(today, last_day, zero_filled):
    """The day the response was saved on is all nulls, as an unpublished day is."""
    response = load_portal_fixture(LONG_RANGE)
    assert not has_data(split_range_response(response)[SAVED_ON])

    days, filled = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: response}, today=today, assembly=_assembly(),
        zero_fill_stale=True, grace_days=3,
    )

    assert days[-1][0] == last_day
    assert filled == zero_filled
    if zero_filled:
        values = days[-1][1][STAT_KEY_ENERGY_CONSUMED]["values"]
        assert len(values) == 24
        assert all(item["value"] == 0.0 for entry in values for item in entry["items"])


@pytest.mark.parametrize("skip_leading_gaps,first_day,zero_filled_days", [
    pytest.param(True, date(2026, 3, 19), 0, id="history-starts-at-first-data"),
    pytest.param(False, date(2026, 3, 1), 18, id="leading-gap-zero-filled"),
])
def test_unknown_assembly_falls_back_to_the_first_day_with_data(
    skip_leading_gaps, first_day, zero_filled_days
):
    """Without the assembly date the leading null days decide where history starts."""
    days, zero_filled = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: load_portal_fixture(LONG_RANGE)},
        today=SAVED_ON + timedelta(days=30), zero_fill_stale=True, grace_days=3,
        skip_leading_gaps=skip_leading_gaps,
    )

    assert days[0][0] == first_day
    assert len([day for day in zero_filled if day < date(2026, 3, 19)]) == zero_filled_days


# --- all measurement types over the spring-forward week ------------------------


def test_spring_forward_week_merges_every_measurement_type():
    """Each type keeps 23 hours on the switch day, without 02:00, under one date."""
    responses = {key: load_portal_fixture(name) for key, name in SPRING_WEEK.items()}

    days, zero_filled = assemble_days(responses, today=SAVED_ON, assembly=_assembly())

    assert zero_filled == []
    assert [day for day, _ in days] == [date(2026, 3, 27) + timedelta(days=i) for i in range(7)]
    for day, day_data in days:
        assert set(day_data) == set(SPRING_WEEK)
        for key, response in day_data.items():
            hours = [hour.hour for hour in _hours(response)]
            expected = [0, 1, *range(3, 24)] if day == SPRING_FORWARD else list(range(24))
            assert hours == expected, (day, key)


async def test_spring_forward_week_reaches_the_statistics(monkeypatch):
    """Energy is a running total of every hour; power is stored hour by hour as a mean.

    The meter has no generation, so energy and power returned are zeroes —
    real data, stored as such.
    """
    responses = {key: load_portal_fixture(name) for key, name in SPRING_WEEK.items()}
    days, _ = assemble_days(responses, today=SAVED_ON, assembly=_assembly())
    writes = Mock()
    monkeypatch.setattr(statistics, "async_add_external_statistics", writes)
    monkeypatch.setattr(statistics, "_newest_entry", AsyncMock(return_value=(None, 0.0)))

    await statistics.async_insert_historical_statistics(FakeHass(), FAKE_PPE, days)

    written = {call.args[1]["name"]: call.args[2] for call in writes.call_args_list}
    assert set(written) == {
        f"{prefix} {direction}{zone}"
        for prefix in ("Energia", "Moc")
        for direction in ("pobrana", "oddana")
        for zone in ("", " – Dzień", " – Noc")
    }
    hours = 7 * 24 - 1
    assert len(written["Energia pobrana"]) == len(written["Moc pobrana"]) == hours
    assert len(written["Energia pobrana – Dzień"]) + len(
        written["Energia pobrana – Noc"]
    ) == 2 * hours  # every hour is in both zone series, with a zero in the other zone

    consumed = responses[STAT_KEY_ENERGY_CONSUMED]
    total = written["Energia pobrana"][-1]["sum"]
    assert total == pytest.approx(sum(zone["consumption"] for zone in consumed["zones"]), abs=1e-3)
    assert written["Energia oddana"][-1]["sum"] == 0.0
    assert all(row["mean"] >= 0 for row in written["Moc pobrana"])
    assert all(row["mean"] == 0.0 for row in written["Moc oddana"])


# --- the fall-back day ---------------------------------------------------------


@pytest.mark.parametrize("name", FALL_BACK_RANGES)
def test_fall_back_day_gets_its_repeated_hour(name):
    """The day clocks go back has 25 consecutive hours, 02:00 twice.

    The portal returns the second 02:00–03:00 apart, in 'valuesWinterToTable',
    and dates the first 01:00–02:00 and 02:00–03:00 an hour late.  Taken as
    they come, 01:00 was missing, the first 02:00 collided with the repeated
    one and the repeated hour was lost.
    """
    response = load_portal_fixture(name)
    assert len(response["valuesWinterToTable"]) == 1
    repeated = response["valuesWinterToTable"][0]

    days = split_range_response(response)

    day = days[FALL_BACK]
    hours = _hours(day)
    midnight = dt_util.start_of_local_day(FALL_BACK).timestamp()
    # Timestamps, not datetimes: local datetimes compare by wall clock, so the
    # two 02:00 would pass for one and the same hour.
    assert [hour.timestamp() for hour in hours] == [midnight + i * 3600 for i in range(25)]
    assert [hour.hour for hour in hours] == [0, 1, 2, 2, *range(3, 24)]
    assert [entry["timeId"] for entry in day["values"]] == [1, 2, 3, 3, *range(4, 25)]
    assert day["values"][3]["items"] == repeated["items"]
    for other, other_data in days.items():
        if other != FALL_BACK:
            assert [hour.hour for hour in _hours(other_data)] == list(range(24)), other


@pytest.mark.parametrize("name", FALL_BACK_RANGES)
def test_fall_back_hours_add_up_to_the_portals_own_sums(name):
    """Every hour of the range, the repeated one too, reaches the series once."""
    response = load_portal_fixture(name)
    zone_names = {zone["id"]: zone["name"] for zone in response["zones"]}
    days, _ = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: response}, today=FALL_BACK_SAVED_ON, assembly=_assembly()
    )
    series: dict[str, list[tuple[datetime, float]]] = {}
    for _, day_data in days:
        statistics._collect_series(day_data[STAT_KEY_ENERGY_CONSUMED], "pobrana", "Energia", series)

    for rows in series.values():
        assert len({hour.timestamp() for hour, _ in rows}) == len(rows)
    assert sum(v for _, v in series["Energia pobrana"]) == pytest.approx(
        response["sumConsumption"], abs=1e-3
    )
    for zone in response["zones"]:
        rows = series[f"Energia pobrana – {zone_names[zone['id']]}"]
        assert sum(v for _, v in rows) == pytest.approx(zone["consumption"], abs=1e-3)


async def test_coordinator_fetches_the_week_with_one_request_per_type(monkeypatch):
    """The coordinator asks for each type once and returns what assemble_days makes of it."""
    by_type = {
        MeasurementType.ENERGY_CONSUMED: SPRING_WEEK[STAT_KEY_ENERGY_CONSUMED],
        MeasurementType.POWER_CONSUMED: SPRING_WEEK[STAT_KEY_POWER_CONSUMED],
        MeasurementType.ENERGY_RETURNED: SPRING_WEEK[STAT_KEY_ENERGY_RETURNED],
        MeasurementType.POWER_RETURNED: SPRING_WEEK[STAT_KEY_POWER_RETURNED],
    }
    coordinator = object.__new__(coordinator_module.EneaUpdateCoordinator)
    coordinator._prosumer = False
    coordinator._fetch_consumption = coordinator._fetch_generation = True
    coordinator._fetch_power_consumption = coordinator._fetch_power_generation = True
    coordinator._assembly_datetime = _assembly()
    coordinator._zero_filled_days = set()
    coordinator._meter_code = FAKE_PPE
    coordinator.meter_id = FAKE_METER_ID
    coordinator.client = SimpleNamespace(get_consumption_data_range=AsyncMock(
        side_effect=lambda meter_id, first, last, mtype, resolution, source: (
            load_portal_fixture(by_type[mtype])
        )
    ))
    monkeypatch.setattr(
        coordinator_module.dt_util, "now",
        lambda: datetime.combine(SAVED_ON, time(9), dt_util.DEFAULT_TIME_ZONE),
    )

    days = await coordinator._fetch_days_forward(date(2026, 3, 27), date(2026, 4, 2), True, 3)

    assert coordinator.client.get_consumption_data_range.await_count == 4
    expected, _ = assemble_days(
        {key: load_portal_fixture(name) for key, name in SPRING_WEEK.items()},
        today=SAVED_ON, assembly=_assembly(),
    )
    assert days == expected
    assert coordinator._zero_filled_days == set()


# --- costs --------------------------------------------------------------------


class _Pricing:
    """Per-kWh prices of one zone, as enea_prices' ZonePricing exposes them."""

    oze = 0.0073
    cogeneration = 0.003

    def __init__(self, energy: float, total_distribution: float) -> None:
        self.energy = energy
        self.total_distribution = total_distribution


class _G12Period:
    """Enea's G12 schedule: night 22–6 and 13–15 all year, as the portal shows it."""

    NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5, 13, 14})

    def __init__(self) -> None:
        self.zones = {"Dzień": _Pricing(0.6, 0.4), "Noc": _Pricing(0.4, 0.1)}

    def get_zone_at_hour(self, hour: int, day: date | None = None) -> str:
        """Return the zone an hour falls in; the day does not matter in G12."""
        return "Noc" if hour in self.NIGHT_HOURS else "Dzień"


class _G12Tariff:
    """A G12 tariff whose price table starts on a given day."""

    name = "G12"

    def __init__(self, priced_from: date) -> None:
        self.priced_from = priced_from
        self.period = _G12Period()
        self.periods = [self.period]

    def get_period_for_date(self, day: date) -> _G12Period | None:
        """Return the only period from the start of the table on, None before it."""
        return self.period if day >= self.priced_from else None


def test_costs_follow_the_portals_own_zones():
    """Costing each hour by the G12 schedule charges exactly the kWh the portal puts in each zone.

    The portal reports every hour's kWh under the zone it belongs to; costs
    take the hour's total and pick the zone from the tariff's schedule.  On a
    real G12 meter the two agree hour for hour, so each zone's cost is its
    kWh at its price.
    """
    response = load_portal_fixture(LONG_RANGE)
    zone_names = {zone["id"]: zone["name"] for zone in response["zones"]}
    days, _ = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: response}, today=SAVED_ON, assembly=_assembly()
    )
    tariff = _G12Tariff(priced_from=date(2026, 1, 1))

    result = costs.cost_series(days, STAT_KEY_ENERGY_CONSUMED, tariff, akcyza=0.005)

    kwh_by_zone: dict[str, float] = defaultdict(float)
    for _, day_data in days:
        for entry in day_data[STAT_KEY_ENERGY_CONSUMED]["values"]:
            hour = slot_start_dt(entry).hour
            for item in entry["items"]:
                if item["value"] is not None:
                    zone = zone_names[item["tarifZoneId"]]
                    assert tariff.period.get_zone_at_hour(hour) == zone, (slot_start_dt(entry), zone)
                    kwh_by_zone[zone] += item["value"]

    assert result.days_without_period == {}
    assert len(result.hours) == sum(len(d[STAT_KEY_ENERGY_CONSUMED]["values"]) for _, d in days)
    assert sum(len(series) for series in result.by_zone.values()) == len(result.hours)
    for zone, pricing in tariff.period.zones.items():
        price = costs._kwh_price(pricing, 0.005)
        assert sum(cost for _, cost in result.by_zone[zone]) == pytest.approx(
            kwh_by_zone[zone] * price, abs=1e-6
        )


def test_days_before_the_price_table_are_reported_not_costed():
    """Hours before the table starts still count for a rewrite but get no cost."""
    days, _ = assemble_days(
        {STAT_KEY_ENERGY_CONSUMED: load_portal_fixture(LONG_RANGE)},
        today=SAVED_ON, assembly=_assembly(),
    )

    result = costs.cost_series(
        days, STAT_KEY_ENERGY_CONSUMED, _G12Tariff(priced_from=date(2026, 4, 1)), akcyza=0.0
    )

    assert result.days_without_period == {
        "G12": {date(2026, 3, 20) + timedelta(days=i) for i in range(12)}
    }
    assert min(hour for series in result.by_zone.values() for hour, _ in series).date() == date(2026, 4, 1)
    assert min(result.hours) == datetime.combine(date(2026, 3, 20), time(12), dt_util.DEFAULT_TIME_ZONE)


def test_returned_energy_of_a_meter_without_generation_costs_nothing():
    """Zeroes for energy returned are data, and their cost is zero in every hour."""
    days, _ = assemble_days(
        {key: load_portal_fixture(SPRING_WEEK[key]) for key in (STAT_KEY_ENERGY_CONSUMED, STAT_KEY_ENERGY_RETURNED)},
        today=SAVED_ON, assembly=_assembly(),
    )

    result = costs.cost_series(
        days, STAT_KEY_ENERGY_RETURNED, _G12Tariff(date(2026, 1, 1)), akcyza=0.0, ratio=0.8
    )

    assert len(result.hours) == 7 * 24 - 1
    assert all(cost == 0.0 for series in result.by_zone.values() for _, cost in series)


# --- the dashboard ------------------------------------------------------------


def test_dashboard_sensors():
    """Every sensor reads its state and attributes from the real dashboard."""
    dashboard = load_portal_fixture("dashboard")
    states = {
        description.key: (
            description.value_fn(dashboard),
            description.attr_fn(dashboard) if description.attr_fn else None,
        )
        for description in SENSOR_DESCRIPTIONS
    }

    assert states["tariff"] == ("G12", {"zones": ["Dzień 1.8.1", "Noc 1.8.2"]})
    assert states["capacity"] == (14, None)
    assert states["status"][0] == "Aktywny_Pod napięciem - z układem pomiarowym"
    assert states["address"][0] == "Przykładowa 1, 60-001, Poznań"
    assert states["meter_model"] == ("OTUS3", {"assembly_date": "2026-03-20T11:12:44+00:00"})
    assert states["meter_assembly"] == (_assembly(), None)
    assert states["phases"] == ("three_phase", {"source": "meter_model"})
    assert states["han_wmbus"][0] == states["han_p1"][0] == "inactive"
    assert states["switch_state"] == ("on", {})
    assert states["billing_period_start"] == (
        date(2026, 8, 6), {"period_starts": ["2026-06-07", "2026-08-06"]}
    )
    reading = states["reading_date"][0]
    assert reading.astimezone(dt_util.DEFAULT_TIME_ZONE).date() == SAVED_ON
    assert {d.key: d.value_fn(dashboard) for d in BINARY_SENSOR_DESCRIPTIONS} == {
        "transmission": True, "han_available": True,
    }


def test_dashboard_billing_and_installation():
    """The bimonthly billing cycle and the next reading come out of billingWeekData."""
    dashboard = load_portal_fixture("dashboard")
    starts = billing_period_starts(dashboard)

    assert starts == [date(2026, 6, 7), date(2026, 8, 6)]
    assert detect_billing_months(starts, None, None) == (2, "billing_periods")
    assert latest_reading_day(starts, None, SAVED_ON - timedelta(days=1), 2) == (
        date(2026, 10, 5), "billing_cycle",
    )
    assert agreement_tariffs(dashboard) == [
        (date(2008, 4, 18), date(2024, 12, 7), "G12"),
        (date(2024, 12, 7), None, "G12"),
    ]
    assert infer_phases(dashboard) == ("three_phase", "meter_model")
    assert [zone_name(label) for label in dashboard["currentValues"][0]["ppeZones"]] == [
        "Dzień", "Noc",
    ]


def test_diagnostics_hide_the_customer_but_keep_the_zones():
    """The report keeps zone ids and active energy only, and nothing that identifies."""
    redacted = _redact_meter_data(load_portal_fixture("dashboard"))
    assert redacted is not None
    text = repr(redacted)

    assert FAKE_PPE not in text and "Przykładowa" not in text
    assert all(meter["id"] == "**REDACTED**" for meter in redacted["meters"])
    assert all(agreement["agreementNumber"] == "**REDACTED**" for agreement in redacted["agreements"])
    assert [m["measurementId"] for m in redacted["billingWeekData"]] == [1, 2]
    assert {zone["id"] for zone in redacted["billingWeekData"][0]["zones"]} == {
        zone["id"] for zone in load_portal_fixture(LONG_RANGE)["zones"]
    }


async def test_config_flow_with_the_real_meter_list(monkeypatch):
    """A single consumer meter goes straight to the options, without net metering."""
    client = SimpleNamespace(
        authenticate=AsyncMock(),
        get_meters=AsyncMock(return_value=load_portal_fixture("user_ppes")),
        get_ppe_dashboard=AsyncMock(return_value=load_portal_fixture("dashboard")),
    )
    monkeypatch.setattr(config_flow, "async_create_clientsession", Mock(
        return_value=SimpleNamespace(close=AsyncMock())
    ))
    monkeypatch.setattr(config_flow, "EneaApiClient", Mock(return_value=client))
    flow = config_flow.EneaConfigFlow()
    flow.handler, flow.flow_id, flow.context = "enea", "test-flow", {"source": "user"}
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entry_for_domain_unique_id=Mock(return_value=None),
    ))
    monkeypatch.setattr(flow, "_async_in_progress", Mock(return_value=[]))

    result = await flow.async_step_user({"username": "test@example.invalid", "password": "x"})

    assert (result["type"], result["step_id"]) == ("form", "configure")
    assert "net_metering" not in {str(key) for key in result["data_schema"].schema}
    assert flow._meter_label(flow._meters[0]) == (
        f"{FAKE_PPE} (G12) – Przykładowa 1, 60-001, Poznań"
    )


# --- the fixtures themselves ---------------------------------------------------


def test_fixtures_carry_only_made_up_identifiers():
    """Every PPE-like number is the made-up one and the ids are the anonymiser's."""
    for path in PORTAL_FIXTURES.glob("*.json"):
        assert set(re.findall(r"\d{18}", path.read_text(encoding="utf-8"))) <= {FAKE_PPE}, path.name

    dashboard = load_portal_fixture("dashboard")
    assert (dashboard["id"], dashboard["code"], dashboard["name"]) == (FAKE_METER_ID, FAKE_PPE, FAKE_PPE)
    assert [meter["id"] for meter in dashboard["meters"]] == [1001, 1002, 1003]
    assert all(meter["serialNumber"].startswith("9000000") for meter in dashboard["meters"])
    assert all(a["agreementNumber"].startswith("UMOWA/TEST/") for a in dashboard["agreements"])
    assert [(p["id"], p["code"]) for p in load_portal_fixture("user_ppes")] == [(FAKE_METER_ID, FAKE_PPE)]
