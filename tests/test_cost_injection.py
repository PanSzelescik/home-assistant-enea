"""Turning fetched days into cost series, and what happens to un-priced days."""
from __future__ import annotations

import datetime
import logging
from typing import Any

import pytest

from custom_components.enea import costs
from custom_components.enea.const import STAT_KEY_ENERGY_CONSUMED

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


def _day(day: datetime.date) -> tuple[datetime.date, dict[str, Any]]:
    """One fetched day holding a single hourly slot with a reading."""
    end = datetime.datetime(day.year, day.month, day.day, 1, tzinfo=TZ)
    return (
        day,
        {
            STAT_KEY_ENERGY_CONSUMED: {
                "values": [
                    {
                        "integrationEnd": end.timestamp() * 1000,
                        "items": [{"value": 2.0}],
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
