"""Billing period starts are read from the dashboard's billingWeekData segments."""
from __future__ import annotations

from datetime import date
from typing import Any

# Segment boundaries (timeFrom, timeTo) of a real dashboard response.  The invoice
# for that meter covered 07/06/2026–05/08/2026, so periods start on 06-07 and 08-06.
_SEGMENTS = [
    (1775512800000, 1780351200000),  # 2026-04-07 → 06-02: data window start
    (1780351200000, 1780437600000),  # 06-02 → 06-03: daily
    (1780437600000, 1780524000000),
    (1780524000000, 1780610400000),
    (1780610400000, 1780696800000),
    (1780696800000, 1780783200000),
    (1780783200000, 1785621600000),  # 06-07 → 08-02: billing period
    (1785621600000, 1785708000000),  # 08-02 → 08-03: daily
    (1785708000000, 1785794400000),
    (1785794400000, 1785880800000),
    (1785880800000, 1785967200000),
    (1785967200000, 1790805600000),  # 08-06 → 10-01: current billing period
]


def _dashboard(segments: list[tuple[int, int]]) -> dict[str, Any]:
    """Return a dashboard response carrying the given consumption segments."""
    values = [{"timeFrom": start, "timeTo": end, "items": []} for start, end in segments]
    return {
        "billingWeekData": [
            {"measurementId": 2, "values": []},
            {"measurementId": 1, "values": values},
        ]
    }


def _description() -> Any:
    """Return the billing period start sensor description."""
    from custom_components.enea.sensor import SENSOR_DESCRIPTIONS

    return next(d for d in SENSOR_DESCRIPTIONS if d.key == "billing_period_start")


def test_period_starts_match_the_invoice() -> None:
    """Long segments mark period starts; the data window start is not one."""
    description = _description()
    data = _dashboard(_SEGMENTS)

    assert description.value_fn(data) == date(2026, 8, 6)
    assert description.attr_fn(data) == {"period_starts": ["2026-06-07", "2026-08-06"]}


def test_long_first_segment_after_daily_ones_counts() -> None:
    """Only the very first segment is the window start, not the first long one."""
    data = _dashboard(_SEGMENTS[5:])

    assert _description().attr_fn(data) == {"period_starts": ["2026-06-07", "2026-08-06"]}


def test_without_billing_data() -> None:
    """No segments leave the sensor unknown."""
    description = _description()

    for data in ({}, {"billingWeekData": []}, _dashboard(_SEGMENTS[:6])):
        assert description.value_fn(data) is None
        assert description.attr_fn(data) == {"period_starts": []}
