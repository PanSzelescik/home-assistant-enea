"""Turning fetched days into cost series, and what happens to un-priced days."""
from __future__ import annotations

import datetime
import logging
from typing import Any

import pytest

from custom_components.enea import costs
from custom_components.enea.const import (
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
)

TZ = datetime.UTC


class _Pricing:
    """Per-kWh prices of one zone, as ZonePricing exposes them."""

    energy = 0.5
    total_distribution = 0.3


class _Period:
    """A tariff period with a single all-day zone."""

    def __init__(self) -> None:
        self.zones = {"peak": _Pricing()}

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        """Every hour belongs to the only zone this period has."""
        return "peak"


class _Tariff:
    """A tariff group that only prices the days it was given."""

    name = "G12w"

    def __init__(self, priced: set[datetime.date]) -> None:
        self._priced = priced
        self._period = _Period()

    def get_period_for_date(self, d: datetime.date) -> _Period | None:
        """Return the period for a priced day, None for anything else."""
        return self._period if d in self._priced else None


def _day(
    day: datetime.date, kwh: float = 2.0, key: str = STAT_KEY_ENERGY_CONSUMED
) -> tuple[datetime.date, dict[str, Any]]:
    """One fetched day holding a single hourly slot with a reading."""
    end = datetime.datetime(day.year, day.month, day.day, 1, tzinfo=TZ)
    return (
        day,
        {
            key: {
                "values": [
                    {
                        "integrationEnd": end.timestamp() * 1000,
                        "items": [{"value": kwh}],
                    }
                ]
            }
        },
    )


@pytest.fixture
def injected(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list]]:
    """Capture the series that would be written, without touching a recorder."""
    calls: list[tuple[str, list]] = []

    async def fake_inject(hass: Any, meter_code: str, name: str, series: list) -> float:
        calls.append((name, series))
        return 0.0

    monkeypatch.setattr(costs, "_inject_cost_series", fake_inject)
    return calls


async def test_priced_days_are_injected(injected) -> None:
    """A day the tariff can price is stored, at that zone's rate."""
    day = datetime.date(2026, 3, 2)

    await costs.async_insert_cost_statistics(
        object(), "PPE", [_day(day)], _Tariff({day}), True, False
    )

    assert len(injected) == 1
    # 2 kWh x (0.5 energy + 0.3 distribution) x 1.23 VAT = 2 x 0.984 = 1.968 zl
    assert injected[0][1][0][1] == pytest.approx(1.968)


async def test_days_without_a_tariff_period_are_reported(
    injected, caplog: pytest.LogCaptureFixture
) -> None:
    """Days the tariff cannot price must be reported, not silently dropped.

    The bundled tariff table covers a fixed date range.  Backfilling outside it
    — history older than the first period, or any day once the table runs out —
    left the energy statistics in place and the cost statistics simply absent,
    with nothing in the log to explain the gap.
    """
    caplog.set_level(logging.WARNING)
    priced = datetime.date(2026, 3, 2)
    unpriced = [datetime.date(2025, 12, 30), datetime.date(2025, 12, 31)]

    await costs.async_insert_cost_statistics(
        object(),
        "PPE",
        [_day(d) for d in (*unpriced, priced)],
        _Tariff({priced}),
        True,
        False,
    )

    assert len(injected) == 1, "the priced day is still injected"
    assert len(caplog.records) == 1, "one summary line, not one per day"
    message = caplog.records[0].getMessage()
    assert "2" in message and "2025-12-30" in message and "2025-12-31" in message
    assert "G12w" in message


async def test_no_warning_when_every_day_is_priced(
    injected, caplog: pytest.LogCaptureFixture
) -> None:
    """A run where every day can be priced writes no warning.

    Backfilling several years at once is ordinary, so a warning that also
    fired on healthy runs would bury the one that matters.
    """
    caplog.set_level(logging.WARNING)
    days = [datetime.date(2026, 3, 2), datetime.date(2026, 3, 3)]

    await costs.async_insert_cost_statistics(
        object(), "PPE", [_day(d) for d in days], _Tariff(set(days)), True, False
    )

    assert caplog.records == []


async def test_a_direction_that_only_ever_reads_zero_is_skipped(
    injected, wire_recorder
) -> None:
    """A meter with nothing to return must not get a cost series of zeroes.

    fetch_generation is on by default, and a meter with no solar panels reports
    zeroes for energy returned rather than nulls, which has_data takes for real
    data — a deliberate choice, so that a day of no consumption is imported
    rather than skipped.  Every such hour used to produce a cost of 0.00 PLN,
    one row per hour and growing daily, and put two statistics that are always
    zero next to the real ones in the Energy Dashboard's cost picker.
    """
    wire_recorder(costs, [])
    day = datetime.date(2026, 3, 2)

    await costs.async_insert_cost_statistics(
        object(),
        "PPE",
        [_day(day, kwh=0.0, key=STAT_KEY_ENERGY_RETURNED)],
        _Tariff({day}),
        False,
        True,
    )

    assert injected == []


async def test_a_zero_day_is_still_written_into_a_series_that_exists(
    injected, wire_recorder
) -> None:
    """The guard decides whether a series starts, never whether it continues.

    Two things depend on zeroes being written into a series that already has
    real data.  A meter that does export gets a continuous series across a day
    it exported nothing.  And a multi-day portal outage, imported as zero-filled
    days, still moves the newest-cost date forward — skipping it would leave
    the same range being fetched from the portal again on every refresh until
    real data appeared.
    """
    wire_recorder(costs, [(datetime.datetime(2026, 3, 1, 23, 0, tzinfo=TZ), 50.0)])
    day = datetime.date(2026, 3, 2)

    await costs.async_insert_cost_statistics(
        object(),
        "PPE",
        [_day(day, kwh=0.0, key=STAT_KEY_ENERGY_RETURNED)],
        _Tariff({day}),
        False,
        True,
    )

    assert len(injected) == 1
    assert [cost for _dt, cost in injected[0][1]] == [0.0]


async def test_a_direction_that_reads_zero_on_one_day_only_is_kept(injected) -> None:
    """A day of no export among days with export still belongs to the series.

    Only a direction with nothing but zeroes across the whole run is dropped.
    """
    days = [datetime.date(2026, 3, 2), datetime.date(2026, 3, 3)]

    await costs.async_insert_cost_statistics(
        object(),
        "PPE",
        [
            _day(days[0], kwh=0.0, key=STAT_KEY_ENERGY_RETURNED),
            _day(days[1], kwh=1.5, key=STAT_KEY_ENERGY_RETURNED),
        ],
        _Tariff(set(days)),
        False,
        True,
    )

    assert len(injected) == 1
    assert [cost for _dt, cost in injected[0][1]] == pytest.approx([0.0, 1.476])
