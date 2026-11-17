"""The tariff's fingerprints are worked out once a day, not once a refresh."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.enea.costs import PriceSignatureCache, TariffHistory, price_signatures

FIRST = date(2026, 1, 1)
LAST = date(2026, 3, 31)


class _Pricing:
    """Per-kWh prices of one zone, as enea_prices' ZonePricing exposes them."""

    def __init__(self, energy: float) -> None:
        self.energy = energy
        self.total_distribution = 0.3
        self.oze = 0.007
        self.cogeneration = 0.003


class _Period:
    """A price period with a day and a night zone that counts its zone lookups."""

    def __init__(self, valid_from: date, valid_until: date, energy: float = 0.5) -> None:
        self.valid_from = valid_from
        self.valid_until = valid_until
        self.zones = {"day": _Pricing(energy), "night": _Pricing(energy / 2)}
        self.lookups = 0

    def get_zone_at_hour(self, hour: int, day: date | None = None) -> str:
        """Return the night zone from 22 to 6."""
        self.lookups += 1
        return "night" if hour >= 22 or hour < 6 else "day"


class _Tariff:
    """A tariff group of one or more periods."""

    name = "G12"

    def __init__(self, *periods: _Period) -> None:
        self.periods = list(periods) or [_Period(date(2026, 1, 1), date(2026, 12, 31))]

    def get_period_for_date(self, day: date) -> _Period | None:
        """Return the period in force on the day."""
        return next((p for p in self.periods if p.valid_from <= day <= p.valid_until), None)

    @property
    def lookups(self) -> int:
        """Return how many hours the periods were asked about."""
        return sum(period.lookups for period in self.periods)


def test_cache_returns_what_price_signatures_does():
    """The cached fingerprints are exactly the ones computed directly."""
    tariff = _Tariff()

    assert PriceSignatureCache().get(tariff, FIRST, LAST) == price_signatures(tariff, FIRST, LAST)


def test_same_range_again_computes_nothing():
    """A refresh with the same tariff and range looks no zone up."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    first = cache.get(tariff, FIRST, LAST)
    worked = tariff.lookups

    assert cache.get(tariff, FIRST, LAST) == first
    assert tariff.lookups == worked == 90 * 24


def test_one_more_day_computes_that_day_alone():
    """The day after — what a refresh a day later asks for — costs 24 lookups."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    cache.get(tariff, FIRST, LAST)
    worked = tariff.lookups

    longer = cache.get(tariff, FIRST, LAST + timedelta(days=1))

    assert tariff.lookups == worked + 24
    assert longer == price_signatures(tariff, FIRST, LAST + timedelta(days=1))


def test_earlier_start_computes_the_days_before():
    """A range reaching further back adds the earlier days to what is kept."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    cache.get(tariff, FIRST + timedelta(days=10), LAST)
    worked = tariff.lookups

    wider = cache.get(tariff, FIRST, LAST)

    assert tariff.lookups == worked + 10 * 24
    assert wider == price_signatures(tariff, FIRST, LAST)


def test_part_of_the_range_is_served_from_what_is_kept():
    """A narrower range — the days just costed — returns only its own days."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    cache.get(tariff, FIRST, LAST)
    worked = tariff.lookups

    week = cache.get(tariff, date(2026, 2, 1), date(2026, 2, 7))

    assert tariff.lookups == worked
    assert sorted(week) == [f"2026-02-0{day}" for day in range(1, 8)]
    assert week == price_signatures(tariff, date(2026, 2, 1), date(2026, 2, 7))


def test_range_apart_from_the_kept_one_starts_over():
    """Days far from the kept range are not joined to it across a hole."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    cache.get(tariff, FIRST, date(2026, 1, 10))

    later = cache.get(tariff, date(2026, 6, 1), date(2026, 6, 5))
    back = cache.get(tariff, FIRST, date(2026, 1, 10))

    assert later == price_signatures(tariff, date(2026, 6, 1), date(2026, 6, 5))
    assert back == price_signatures(tariff, FIRST, date(2026, 1, 10))


def test_adjacent_range_is_joined():
    """A range starting the day after the kept one extends it."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    cache.get(tariff, FIRST, date(2026, 1, 10))
    worked = tariff.lookups

    cache.get(tariff, date(2026, 1, 11), date(2026, 1, 12))
    both = cache.get(tariff, FIRST, date(2026, 1, 12))

    assert tariff.lookups == worked + 2 * 24
    assert both == price_signatures(tariff, FIRST, date(2026, 1, 12))


def test_prices_changed_in_place_are_noticed():
    """A period that stays the same object but prices differently is computed again."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    before = cache.get(tariff, FIRST, LAST)

    tariff.periods[0].zones["day"].energy = 0.9
    after = cache.get(tariff, FIRST, LAST)

    assert after != before
    assert after == price_signatures(tariff, FIRST, LAST)


def test_new_periods_are_noticed():
    """Reloaded enea_prices hands out new periods: nothing kept is reused."""
    cache = PriceSignatureCache()
    before = cache.get(_Tariff(), FIRST, LAST)

    dearer = _Tariff(_Period(date(2026, 1, 1), date(2026, 12, 31), energy=0.8))
    after = cache.get(dearer, FIRST, LAST)

    assert after != before
    assert after == price_signatures(dearer, FIRST, LAST)


def test_period_added_to_the_table_is_noticed():
    """A table that now reaches further prices days it left out before."""
    cache = PriceSignatureCache()
    january = _Period(date(2026, 1, 1), date(2026, 1, 31))
    assert len(cache.get(_Tariff(january), FIRST, LAST)) == 31

    rest = _Period(date(2026, 2, 1), date(2026, 12, 31))
    assert len(cache.get(_Tariff(january, rest), FIRST, LAST)) == 90


@pytest.mark.parametrize("ratio", [0.8, 0.7])
def test_net_metering_ratio_is_part_of_the_key(ratio):
    """Fingerprints kept for one ratio are not handed out for another."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    plain = cache.get(tariff, FIRST, LAST)

    with_ratio = cache.get(tariff, FIRST, LAST, ratio)

    assert with_ratio != plain
    assert with_ratio == price_signatures(tariff, FIRST, LAST, ratio)
    assert cache.get(tariff, FIRST, LAST) == plain


def test_change_of_the_agreements_is_noticed():
    """The same groups under other agreement dates price some days differently."""
    current, earlier = _Tariff(), _Tariff(_Period(date(2026, 1, 1), date(2026, 12, 31), energy=0.9))
    cache = PriceSignatureCache()
    until_february = TariffHistory(
        current, [(date(2025, 1, 1), date(2026, 2, 1), earlier, "G11"), (date(2026, 2, 1), None, current, "G12")]
    )
    until_march = TariffHistory(
        current, [(date(2025, 1, 1), date(2026, 3, 1), earlier, "G11"), (date(2026, 3, 1), None, current, "G12")]
    )

    first = cache.get(until_february, FIRST, LAST)
    second = cache.get(until_march, FIRST, LAST)

    assert first == price_signatures(until_february, FIRST, LAST)
    assert second == price_signatures(until_march, FIRST, LAST)
    assert first["2026-02-15"] != second["2026-02-15"]
    assert first["2026-01-15"] == second["2026-01-15"]


def test_same_agreements_built_again_reuse_what_is_kept():
    """Every refresh builds a new TariffHistory; equal ones share the kept fingerprints."""
    current = _Tariff()
    cache = PriceSignatureCache()
    spans = [(date(2025, 1, 1), None, current, "G12")]
    cache.get(TariffHistory(current, list(spans)), FIRST, LAST)
    worked = current.lookups

    cache.get(TariffHistory(current, list(spans)), FIRST, LAST)

    assert current.lookups == worked


def test_days_without_prices_stay_out():
    """A day the table does not reach has no fingerprint, kept or not."""
    tariff = _Tariff(_Period(date(2026, 1, 1), date(2026, 1, 31)))
    cache = PriceSignatureCache()

    for _ in range(2):
        found = cache.get(tariff, date(2026, 1, 30), date(2026, 2, 2))
        assert sorted(found) == ["2026-01-30", "2026-01-31"]


def test_empty_range_is_empty():
    """A range that ends before it starts has nothing, and disturbs nothing kept."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    kept = cache.get(tariff, FIRST, LAST)

    assert cache.get(tariff, LAST, FIRST) == {}
    assert cache.get(tariff, FIRST, LAST) == kept


def test_returned_dict_is_not_the_kept_one():
    """The caller may change what it gets without spoiling the next answer."""
    tariff, cache = _Tariff(), PriceSignatureCache()
    found = cache.get(tariff, FIRST, LAST)
    found.clear()

    assert len(cache.get(tariff, FIRST, LAST)) == 90
