"""Working out the enea_prices settings from the meter data and the statistics."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import installation

READING = date(2026, 8, 5)
"""The latest billing reading: the current period starts on 6 August."""


def _ms(day: date) -> int:
    """Return local midnight of a day as the portal's millisecond timestamp."""
    return int(dt_util.start_of_local_day(day).timestamp() * 1000)


def _dashboard(
    period_starts: list[date],
    assembled: date = date(2023, 3, 1),
    replaced: bool = False,
) -> dict[str, Any]:
    """A dashboard response: billing periods starting on the given days, one meter.

    replaced adds the meter the active one took over from on its assembly day.
    """
    window_start = period_starts[0] - timedelta(days=40) if period_starts else date(2026, 4, 7)
    segments = [(window_start, window_start + timedelta(days=30))]
    for start in period_starts:
        segments.append((start - timedelta(days=1), start))  # a daily segment
        segments.append((start, start + timedelta(days=55)))
    meters = [{"typeName": "OTUS3", "assemblyDate": _ms(assembled)}]
    if replaced:
        meters.append(
            {"typeName": "OTUS3", "assemblyDate": _ms(date(2015, 1, 1)), "disassemblyDate": _ms(assembled)}
        )
    return {
        "meters": meters,
        "billingWeekData": [
            {
                "measurementId": 1,
                "values": [
                    {"timeFrom": _ms(a), "timeTo": _ms(b), "items": []} for a, b in segments
                ],
            }
        ],
    }


def _hour(day: date) -> datetime:
    """The last hour of a local day."""
    return datetime.combine(day, time(23), tzinfo=dt_util.DEFAULT_TIME_ZONE)


@pytest.mark.parametrize(
    ("kwh", "bracket"),
    [(0, 0), (499.9, 0), (500, 1), (1200, 1), (1200.1, 2), (2800, 2), (2800.1, 3)],
)
def test_capacity_brackets_follow_the_tariff(kwh: float, bracket: int) -> None:
    """Below 500, from 500 to 1200, above 1200 to 2800, above 2800 (pkt 3.1.29)."""
    assert installation.capacity_bracket(kwh) == bracket


def test_billing_months_come_from_the_portals_periods() -> None:
    starts = [date(2026, 6, 6), date(2026, 8, 6)]

    assert installation.detect_billing_months(starts, None, None) == (2, "billing_periods")


def test_billing_months_fall_back_to_the_reading_dates() -> None:
    """A yearly period leaves at most one boundary in the half-year window."""
    months = installation.detect_billing_months(
        [date(2026, 8, 6)], date(2025, 8, 5), READING
    )

    assert months == (12, "reading_dates")


def test_a_gap_no_tariff_bills_by_settles_nothing() -> None:
    starts = [date(2026, 5, 6), date(2026, 8, 6)]

    assert installation.detect_billing_months(starts, None, None) == (None, None)


def test_a_boundary_that_is_no_reading_settles_nothing() -> None:
    """A prosumer's portal split the period at a tariff change and at the new year.

    The 20 days between them rounded to one month, and Repairs suggested
    changing a yearly billing period to a monthly one.
    """
    starts = [date(2025, 2, 25), date(2025, 12, 12), date(2026, 1, 1)]

    assert installation.detect_billing_months(starts, None, None) == (None, None)


def test_the_later_of_the_two_readings_counts() -> None:
    """The tariff counts the year up to the latest reading (pkt 3.1.30)."""
    starts = [date(2026, 6, 6), date(2026, 8, 6)]

    assert installation.latest_reading_day(starts, date(2026, 6, 5), None) == (
        READING,
        "billing_periods",
    )
    assert installation.latest_reading_day([], None, date(2026, 10, 8)) == (
        date(2026, 10, 8),
        "last_365_days",
    )


def test_readings_since_the_portals_window_are_counted_by_the_cycle() -> None:
    """The window ended on 30 September; billed every 2 months, 5 October was read too."""
    starts = [date(2026, 6, 6), date(2026, 8, 6)]

    assert installation.latest_reading_day(starts, None, date(2026, 10, 8), 2) == (
        date(2026, 10, 5),
        "billing_cycle",
    )
    assert installation.latest_reading_day(starts, None, date(2026, 10, 4), 2) == (
        READING,
        "billing_periods",
    )


@pytest.fixture
def detect(wire_recorder):
    """Run the detection over energy totals stored at the end of the given days."""

    async def _run(
        totals: dict[date, float], data: dict[str, Any], last_reading: date | None = None
    ) -> Any:
        wire_recorder(installation, [(_hour(day), total) for day, total in totals.items()])
        return await installation.async_detect_installation(
            object(), "PPE", data, None, last_reading, max(totals)
        )

    return _run


PERIODS = [date(2026, 6, 6), date(2026, 8, 6)]


async def test_the_year_ends_on_the_latest_reading(detect) -> None:
    detected = await detect(
        {date(2025, 8, 4): 900.0, date(2025, 8, 5): 1000.0, READING: 4170.0},
        _dashboard(PERIODS),
    )

    assert detected.annual_kwh == pytest.approx(3170.0)
    assert detected.annual_kwh_until == READING
    assert detected.annual_kwh_source == "billing_periods"
    assert detected.billing_months == 2
    assert detected.phases == 3


async def test_the_year_ends_on_the_reading_the_cycle_brings(detect) -> None:
    detected = await detect(
        {date(2025, 10, 5): 1000.0, READING: 3000.0, date(2026, 10, 5): 4170.0, date(2026, 10, 8): 4300.0},
        _dashboard(PERIODS),
    )

    assert detected.annual_kwh == pytest.approx(3170.0)
    assert detected.annual_kwh_until == date(2026, 10, 5)
    assert detected.annual_kwh_source == "billing_cycle"


async def test_a_new_connection_counts_everything_used_so_far(detect) -> None:
    """Less than a year of use qualifies by the whole of it (pkt 3.1.31)."""
    detected = await detect(
        {date(2026, 3, 2): 5.0, READING: 905.0},
        _dashboard(PERIODS, assembled=date(2026, 3, 1)),
    )

    assert detected.annual_kwh == pytest.approx(905.0)
    assert not detected.annual_kwh_partial


async def test_zero_filled_days_before_the_portals_history_are_no_year(detect) -> None:
    """Zeros stored from the assembly up to where the portal's data begins.

    Older versions zero-filled those days, so the statistics seemed to reach a
    year back and the consumption since March passed for a whole year's.
    """
    detected = await detect(
        {date(2025, 8, 4): 0.0, date(2025, 8, 5): 0.0, date(2026, 3, 2): 5.0, READING: 905.0},
        _dashboard(PERIODS),
    )

    assert detected.annual_kwh is None


async def test_a_meter_replaced_within_the_year_settles_nothing(detect) -> None:
    """The statistics only hold the new meter; the customer used more."""
    detected = await detect(
        {date(2026, 3, 2): 5.0, READING: 905.0},
        _dashboard(PERIODS, assembled=date(2026, 3, 1), replaced=True),
    )

    assert detected.annual_kwh is None
    assert detected.annual_kwh_until is None


async def test_statistics_from_before_the_replacement_cover_the_year(detect) -> None:
    """An Enea entry older than the new meter holds the old one's data too."""
    detected = await detect(
        {date(2025, 8, 4): 900.0, date(2025, 8, 5): 1000.0, READING: 1905.0},
        _dashboard(PERIODS, assembled=date(2026, 3, 1), replaced=True),
    )

    assert detected.annual_kwh == pytest.approx(905.0)
    assert not detected.annual_kwh_partial


async def test_a_replaced_meter_past_the_top_limit_still_settles_the_bracket(detect) -> None:
    """Half a year on the new meter is over 2800 kWh: the whole year can only be more."""
    detected = await detect(
        {date(2026, 3, 21): 10.0, READING: 3217.0},
        _dashboard(PERIODS, assembled=date(2026, 3, 20), replaced=True),
    )

    assert detected.annual_kwh == pytest.approx(3217.0)
    assert detected.annual_kwh_partial
    assert installation.capacity_bracket(detected.annual_kwh) == 3


async def test_without_the_reading_date_the_cycle_still_settles_the_bracket(detect) -> None:
    """The case of a meter replaced in March 2026 (MT174 → OTUS3), set up anew.

    Without an enea_prices entry there are no reading date entities, so the
    latest reading known was the portal's 5 August — and the year up to it,
    short of its start, held only 2200 kWh, settling nothing.  The reading the
    2-month cycle brings on 5 October holds 3190 kWh: past the top limit.
    """
    detected = await detect(
        {
            date(2025, 12, 1): 10.0,
            READING: 2200.0,
            date(2026, 10, 5): 3190.0,
            date(2026, 10, 8): 3207.0,
        },
        _dashboard(PERIODS, assembled=date(2026, 3, 20), replaced=True),
    )

    assert detected.annual_kwh == pytest.approx(3190.0)
    assert detected.annual_kwh_until == date(2026, 10, 5)
    assert detected.annual_kwh_partial


async def test_statistics_short_of_the_reading_settle_nothing(detect) -> None:
    detected = await detect(
        {date(2025, 8, 5): 1000.0, date(2026, 7, 30): 4000.0},
        _dashboard(PERIODS),
    )

    assert detected.annual_kwh is None

