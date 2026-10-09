"""The installation facts enea_prices prices the monthly fees with, read off the meter.

The enea_prices integration asks the user for three facts about the
installation: the number of phases (fixed network fee), the length of the
billing period (subscription fee) and the yearly consumption (capacity fee).
The Portal Odbiorcy Enea data and the energy statistics settle most of them, so
this works them out — each with where it came from — for them to be shown next
to what was typed in, offered when enea_prices is set up and checked later.

A fact the data does not settle is None.  Nothing here changes a setting.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .connector import billing_period_starts, get_active_meter, infer_phases
from .const import (
    AVERAGE_MONTH_DAYS,
    BILLING_PERIOD_MONTHS,
    CAPACITY_BRACKET_LIMITS_KWH,
    EPOCH,
    INSTALLATION_SOURCE_BILLING_CYCLE,
    INSTALLATION_SOURCE_BILLING_PERIODS,
    INSTALLATION_SOURCE_LAST_365_DAYS,
    INSTALLATION_SOURCE_READING_DATES,
    NEW_CONNECTION_STATISTICS_SLACK,
    PHASES_COUNT,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_NAME_BY_KEY,
)
from .statistics import get_statistic_id


@dataclass(frozen=True)
class DetectedInstallation:
    """Installation facts worked out from the meter, each with its source.

    annual_kwh is the consumption the capacity fee is charged by: the year
    ending on annual_kwh_until, the day of the latest reading (see
    async_detect_installation).  annual_kwh_partial marks only part of that
    year measured — a meter replaced within it — where the sum is a lower
    bound, kept only when it settles the bracket all the same.
    """

    phases: int | None = None
    phases_source: str | None = None
    billing_months: int | None = None
    billing_months_source: str | None = None
    annual_kwh: float | None = None
    annual_kwh_source: str | None = None
    annual_kwh_until: date | None = None
    annual_kwh_partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the facts for the diagnostics report."""
        until = self.annual_kwh_until
        return {
            "phases": self.phases,
            "phases_source": self.phases_source,
            "billing_months": self.billing_months,
            "billing_months_source": self.billing_months_source,
            "annual_kwh": None if self.annual_kwh is None else round(self.annual_kwh, 1),
            "annual_kwh_source": self.annual_kwh_source,
            "annual_kwh_until": until.isoformat() if until is not None else None,
            "annual_kwh_partial": self.annual_kwh_partial,
            "capacity_bracket": (
                None if self.annual_kwh is None else capacity_bracket(self.annual_kwh)
            ),
        }


def capacity_bracket(annual_kwh: float) -> int:
    """Return the capacity fee bracket of a yearly consumption, 0 (lowest) to 3.

    Below 500 kWh, from 500 to 1200, above 1200 to 2800, above 2800 — the same
    boundaries enea_prices prices the fee by.
    """
    lowest, middle, upper = CAPACITY_BRACKET_LIMITS_KWH
    if annual_kwh < lowest:
        return 0
    if annual_kwh <= middle:
        return 1
    if annual_kwh <= upper:
        return 2
    return 3


def _months(first: date, last: date) -> int | None:
    """Return the billing period length a span of days stands for, if a tariff has one."""
    months = round((last - first).days / AVERAGE_MONTH_DAYS)
    return months if months in BILLING_PERIOD_MONTHS else None


def detect_billing_months(
    starts: list[date], prev_reading: date | None, last_reading: date | None
) -> tuple[int | None, str | None]:
    """Return the billing period length in months and where it came from.

    The two latest billing period starts in billingWeekData come first, as the
    Portal Odbiorcy Enea's own view.  Otherwise the reading dates the user set
    for the bill estimate.  A gap no tariff has a subscription fee for settles
    nothing.
    """
    if len(starts) >= 2 and (months := _months(starts[-2], starts[-1])) is not None:
        return months, INSTALLATION_SOURCE_BILLING_PERIODS
    if prev_reading is not None and last_reading is not None and prev_reading < last_reading:
        if (months := _months(prev_reading, last_reading)) is not None:
            return months, INSTALLATION_SOURCE_READING_DATES
    return None, None


def _add_months(day: date, months: int) -> date:
    """Return the day a number of months later, on the month's last day if it is shorter."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def latest_reading_day(
    starts: list[date],
    last_reading: date | None,
    statistics_until: date | None,
    billing_months: int | None = None,
) -> tuple[date | None, str | None]:
    """Return the day of the latest billing reading and where it came from.

    A billing period starts the day after its opening reading, so a start in
    billingWeekData stands for a reading the day before.  The portal's window
    ends well before today, though, and rarely shows the latest reading: with
    the billing period length known, the readings since are counted on from
    the latest start, as far as the statistics reach.  The reading date set for
    the bill estimate counts as well; the latest of them wins.  With none, the
    newest day of the statistics stands in, which makes the yearly consumption
    an estimate of the next reading's.
    """
    candidates = []
    if starts:
        candidates.append((starts[-1] - timedelta(days=1), INSTALLATION_SOURCE_BILLING_PERIODS))
        if billing_months is not None and statistics_until is not None:
            start = starts[-1]
            while (following := _add_months(start, billing_months)) - timedelta(
                days=1
            ) <= statistics_until:
                start = following
            if start != starts[-1]:
                candidates.append((start - timedelta(days=1), INSTALLATION_SOURCE_BILLING_CYCLE))
    if last_reading is not None:
        candidates.append((last_reading, INSTALLATION_SOURCE_READING_DATES))
    if candidates:
        return max(candidates)
    if statistics_until is not None:
        return statistics_until, INSTALLATION_SOURCE_LAST_365_DAYS
    return None, None


def _year_before(day: date) -> date:
    """Return the same day a year earlier (28 February for a 29th)."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _year_complete(data: dict[str, Any], since: date, first_day: date | None) -> bool:
    """Return True when the statistics hold the customer's whole consumption from since on.

    They do when they reach back to since.  They do as well for a new
    connection — the active meter assembled after since, no earlier meter, and
    the statistics starting with it — which the tariff qualifies by everything
    used so far (pkt 3.1.31), exactly their sum.  Otherwise part of the year is
    missing, measured by an earlier meter or before the portal's history
    begins, and the sum can only put the customer too low.
    """
    if first_day is None:
        return False
    if first_day <= since:
        return True
    active = get_active_meter(data)
    if active is None or not active.get("assemblyDate"):
        return False
    assembled = dt_util.as_local(
        dt_util.utc_from_timestamp(active["assemblyDate"] / 1000)
    ).date()
    earlier_meter = any(
        meter is not active and meter.get("disassemblyDate")
        for meter in data.get("meters", [])
    )
    return (
        not earlier_meter
        and since < assembled
        and first_day <= assembled + NEW_CONNECTION_STATISTICS_SLACK
    )


async def _async_consumption(
    hass: HomeAssistant, statistic_id: str, since: date, until: date
) -> tuple[float, date | None]:
    """Return the kWh consumed in (since, until] and the first day of the statistics.

    One daily read of the cumulative sums from the very beginning, as
    async_query_zone_kwh does for the bill: the opening balance is the newest
    sum at or before since, which need not fall on since itself.
    """
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        EPOCH,
        dt_util.start_of_local_day(until + timedelta(days=1)),
        {statistic_id},
        "day",
        None,
        {"sum"},
    )
    days = [
        (dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date(), row.get("sum") or 0.0)
        for row in stats.get(statistic_id, [])
    ]
    opening = next((total for day, total in reversed(days) if day <= since), 0.0)
    closing = next((total for day, total in reversed(days) if day <= until), 0.0)
    return closing - opening, days[0][0] if days else None


async def async_detect_installation(
    hass: HomeAssistant,
    meter_code: str,
    data: dict[str, Any],
    prev_reading: date | None,
    last_reading: date | None,
    statistics_until: date | None,
) -> DetectedInstallation:
    """Work out the installation facts from the dashboard data and the statistics.

    The yearly consumption follows the distribution tariff (pkt 3.1.30): the
    energy used in the year ending on the day of the latest reading.  It is
    read from the energy statistics, so it needs them to reach that day.
    """
    phases, phases_source = infer_phases(data)
    starts = billing_period_starts(data)
    billing_months, billing_source = detect_billing_months(starts, prev_reading, last_reading)

    annual_kwh: float | None = None
    partial = False
    reading_day, annual_source = latest_reading_day(
        starts, last_reading, statistics_until, billing_months
    )
    if (
        reading_day is not None
        and statistics_until is not None
        and reading_day <= statistics_until
    ):
        sid = get_statistic_id(meter_code, STAT_NAME_BY_KEY[STAT_KEY_ENERGY_CONSUMED])
        since = _year_before(reading_day)
        kwh, first_day = await _async_consumption(hass, sid, since, reading_day)
        if _year_complete(data, since, first_day):
            annual_kwh = kwh
        elif first_day is not None and capacity_bracket(kwh) == len(CAPACITY_BRACKET_LIMITS_KWH):
            # Part of the year is missing, so the sum is a lower bound — past
            # the top limit the bracket cannot be any other.
            annual_kwh, partial = kwh, True

    return DetectedInstallation(
        phases=PHASES_COUNT[phases] if phases is not None else None,
        phases_source=phases_source,
        billing_months=billing_months,
        billing_months_source=billing_source,
        annual_kwh=annual_kwh,
        annual_kwh_source=annual_source if annual_kwh is not None else None,
        annual_kwh_until=reading_day if annual_kwh is not None else None,
        annual_kwh_partial=partial,
    )
