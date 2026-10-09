"""What one refresh does about cost statistics that are behind."""
from __future__ import annotations

import datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea import costs as costs_module
from custom_components.enea.connector import EneaApiError
from custom_components.enea.coordinator import EneaUpdateCoordinator

from conftest import FakeStore


@pytest.fixture
def refresh(monkeypatch: pytest.MonkeyPatch, wire_recorder):
    """Run one _async_fetch_and_inject_stats and report what it asked for.

    The coordinator is built without __init__, which needs a running Home
    Assistant.  Everything the method under test reads is set here, and the
    three things it calls out to record their arguments instead of acting.
    """

    def _run(
        energy_reaches: datetime.date,
        portal_has_more: bool,
        catch_up_raises: Exception | None = None,
    ) -> dict[str, list]:
        calls: dict[str, list] = {
            "fetched": [], "injected": [], "cost_checks": [], "events": []
        }

        async def fetch_days_forward(start, end, **kwargs):
            calls["events"].append("fetch")
            calls["fetched"].append((start, end))
            return [(start, {})] if portal_has_more and start <= end else []

        async def inject_days(days):
            calls["events"].append("inject")
            calls["injected"].append(days)

        async def inject_missing_costs(up_to):
            calls["events"].append("cost_check")
            calls["cost_checks"].append(up_to)
            if catch_up_raises is not None:
                raise catch_up_raises

        coord = object.__new__(EneaUpdateCoordinator)
        coord.hass = object()
        coord._meter_code = "PPE"
        coord._fetch_consumption = True
        coord._fetch_generation = False
        coord._fetch_power_consumption = False
        coord._fetch_power_generation = False
        coord._prosumer = False
        coord._backfill_task = None
        coord._fetch_days_forward = fetch_days_forward
        coord._async_inject_days = inject_days
        coord._async_inject_missing_costs = inject_missing_costs

        newest = datetime.datetime.combine(
            energy_reaches, datetime.time(23), tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        wire_recorder(coordinator_module, [(newest, 1.0)])

        recorder = coordinator_module.get_instance(None)

        async def block_till_done() -> None:
            calls["events"].append("recorder_sync")

        recorder.async_block_till_done = block_till_done
        return coord, calls

    return _run


def _yesterday() -> datetime.date:
    """The last day the integration ever asks the portal for."""
    return dt_util.now().date() - datetime.timedelta(days=1)


async def test_costs_are_checked_when_the_portal_has_a_new_day(refresh) -> None:
    """A day arriving for energy must not stop the cost history being filled in.

    Installing enea_prices next to an enea that has been running for months is
    what _async_reload_matching_enea_entries exists for: every day has energy
    statistics and none has costs.  The cost catch-up used to run only on the
    refreshes that found nothing new at the portal.  On a refresh that did find
    a day, that day was written with its costs, the newest cost statistic then
    reached yesterday, and every later refresh saw complete costs and returned
    at once.  The months behind it were never priced, nothing was logged, and
    the state kept itself alive.  Which of the two happens comes down to the
    hour the integration was installed.
    """
    energy_reaches = _yesterday() - datetime.timedelta(days=1)
    coord, calls = refresh(energy_reaches, portal_has_more=True)

    await coord._async_fetch_and_inject_stats()

    assert calls["cost_checks"] == [energy_reaches]
    # The order carries the fix.  Injecting the new day writes its costs too,
    # and from then on the newest cost statistic reaches yesterday — so a
    # catch-up running after it would find nothing left to do.  The recorder
    # drain between them matters just as much: the catch-up only queues its
    # writes, and the new day chains from what a database read returns, so
    # without the drain it would start from a total the recorder has not
    # committed yet and the series would step down where the two writes meet.
    assert calls["events"] == ["cost_check", "fetch", "recorder_sync", "inject"]


async def test_costs_are_checked_when_the_portal_has_nothing_new(refresh) -> None:
    """With no new day to fetch, the cost catch-up still runs."""
    energy_reaches = _yesterday() - datetime.timedelta(days=1)
    coord, calls = refresh(energy_reaches, portal_has_more=False)

    await coord._async_fetch_and_inject_stats()

    assert calls["injected"] == []
    assert calls["cost_checks"] == [energy_reaches]
    assert calls["events"] == ["cost_check", "fetch"]


async def test_a_failing_catch_up_does_not_veto_the_energy_update(
    refresh, caplog: pytest.LogCaptureFixture
) -> None:
    """A portal error in the catch-up must not stop fresh energy being stored.

    The catch-up can reach ranges the portal no longer serves, and a range the
    portal refuses is refused on every refresh.  Running first, an unhandled
    error there would abort the whole cycle before the new day's energy is
    fetched — permanently, since the failure repeats.  The catch-up is best
    effort: it logs and the refresh carries on.
    """
    energy_reaches = _yesterday() - datetime.timedelta(days=1)
    coord, calls = refresh(
        energy_reaches, portal_has_more=True, catch_up_raises=EneaApiError("410")
    )

    await coord._async_fetch_and_inject_stats()

    assert calls["injected"], "the new day must still be injected"
    assert any("catch-up" in r.getMessage().lower() for r in caplog.records)


async def test_a_programming_error_in_the_catch_up_still_propagates(refresh) -> None:
    """Only portal errors are downgraded; a bug must stay loud."""
    energy_reaches = _yesterday() - datetime.timedelta(days=1)
    coord, _calls = refresh(
        energy_reaches, portal_has_more=True, catch_up_raises=ValueError("bug")
    )

    with pytest.raises(ValueError):
        await coord._async_fetch_and_inject_stats()


class _Pricing:
    """Per-kWh prices of one zone, as ZonePricing exposes them."""

    def __init__(self, energy: float = 0.5) -> None:
        self.energy = energy
        self.total_distribution = 0.3


class _Period:
    """A tariff period broad enough to cover any date these tests use."""

    def __init__(self, energy: float = 0.5) -> None:
        self.zones = {"peak": _Pricing(energy)}
        self.valid_from = datetime.date(2000, 1, 1)
        self.valid_until = datetime.date(2099, 12, 31)

    def get_zone_at_hour(self, hour: int, day: datetime.date | None = None) -> str:
        """Every hour belongs to the only zone this period has."""
        return "peak"


class _Tariff:
    """A tariff group with a single all-covering period.

    changed_from, when given, is the first day priced by a second period
    whose energy is dearer — a contract entered with a start in the past.
    """

    def __init__(self, changed_from: datetime.date | None = None) -> None:
        self.periods = [_Period()]
        self._changed_from = changed_from
        self._changed = _Period(energy=0.6)

    def get_period_for_date(self, day: datetime.date) -> _Period | None:
        """Return the period pricing a day."""
        if self._changed_from is not None and day >= self._changed_from:
            return self._changed
        return self.periods[0]


def _cost_coordinator(monkeypatch, insert_costs, fetch_raises=None):
    """A coordinator wired to run _async_inject_missing_costs for real.

    Only the portal fetch and the final insert are stubbed: the fetch answers
    every asked-for day and counts the asks, the insert is the caller's.  The
    tariff is coord.tariff, which a test may replace between refreshes.
    """
    yesterday = dt_util.now().date() - datetime.timedelta(days=1)
    fetches: list[tuple[datetime.date, datetime.date]] = []

    async def fetch_days_forward(start, end, **kwargs):
        fetches.append((start, end))
        if fetch_raises is not None:
            raise fetch_raises
        days = []
        day = start
        while day <= end:
            days.append((day, {}))
            day += datetime.timedelta(days=1)
        return days

    coord = object.__new__(EneaUpdateCoordinator)
    coord.hass = object()
    coord._meter_code = "PPE"
    coord._fetch_consumption = True
    coord._fetch_generation = False
    coord._fetch_power_consumption = False
    coord._fetch_power_generation = False
    coord._prosumer = False
    coord._backfill_task = None
    coord._tariff_name = "G12w"
    coord._dashboard_data = {}
    coord._assembly_datetime = datetime.datetime.combine(
        yesterday - datetime.timedelta(days=30),
        datetime.time(12),
        tzinfo=dt_util.DEFAULT_TIME_ZONE,
    )
    coord._fetch_days_forward = fetch_days_forward
    coord._cost_prices_store = FakeStore()
    coord._cost_prices = None
    coord._cost_prices_loaded = False
    coord._cost_reprice_failed = False
    coord._costs_repriced_from = None
    coord.tariff = _Tariff()
    monkeypatch.setattr(
        coordinator_module, "find_tariff_history", lambda hass, name, data: coord.tariff
    )
    monkeypatch.setattr(
        coordinator_module, "async_insert_cost_statistics", insert_costs
    )
    return coord, yesterday, fetches


async def test_a_meter_of_zeroes_is_checked_once_not_on_every_refresh(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    """A meter whose every direction reads zero must not be refetched for ever.

    Such a meter never grows a cost series — an all-zero direction with
    nothing stored is deliberately not started — so the newest-cost date
    cannot record progress for it.  The coordinator therefore remembers the
    last day the portal answered, and the next refresh asks for nothing.
    Before that, every refresh fetched the whole history from the assembly
    date again.
    """
    wire_recorder(costs_module, [])

    async def insert_costs(*args, **kwargs):
        """What insertion does with zeroes is covered by its own tests."""

    coord, yesterday, fetches = _cost_coordinator(monkeypatch, insert_costs)

    await coord._async_inject_missing_costs(yesterday)
    await coord._async_inject_missing_costs(yesterday)

    assert len(fetches) == 1


async def test_a_range_whose_write_failed_is_asked_for_again(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    """The marker must record the write landing, not the portal answering.

    Moved as soon as the fetch came back, it would sit past a write that
    then failed, and every later refresh would skip the unwritten range —
    the costs would stay missing, silently, until a restart cleared it.
    """
    wire_recorder(costs_module, [])
    failures = [RuntimeError("recorder is gone")]

    async def insert_costs(*args, **kwargs):
        if failures:
            raise failures.pop()

    coord, yesterday, fetches = _cost_coordinator(monkeypatch, insert_costs)

    with pytest.raises(RuntimeError):
        await coord._async_inject_missing_costs(yesterday)
    await coord._async_inject_missing_costs(yesterday)
    await coord._async_inject_missing_costs(yesterday)

    assert len(fetches) == 2, "fetched again after the failure, not after the success"


def _costed_until(day: datetime.date) -> list[tuple[datetime.datetime, float]]:
    """A cost series whose newest entry is the last hour of day."""
    return [
        (datetime.datetime.combine(day, datetime.time(23), tzinfo=dt_util.DEFAULT_TIME_ZONE), 1.0)
    ]


def _costed_coordinator(monkeypatch, wire_recorder, **kwargs):
    """A meter costed up to yesterday, every day at the prices of coord.tariff."""
    inserted: list[tuple[datetime.date, datetime.date]] = []

    async def insert_costs(hass, meter_code, days, tariff, *args, **kwargs):
        inserted.append((days[0][0], days[-1][0]))

    coord, yesterday, fetches = _cost_coordinator(monkeypatch, insert_costs, **kwargs)
    wire_recorder(costs_module, _costed_until(yesterday))
    wire_recorder(coordinator_module, [])
    first = coord._assembly_datetime.date()
    # Looked up leniently so that, run against the code before re-pricing,
    # the tests fail on the costs it leaves alone rather than on this name.
    signatures = getattr(costs_module, "price_signatures", lambda *args: {})
    coord._cost_prices_store.data = {"days": signatures(coord.tariff, first, yesterday)}
    return coord, yesterday, fetches, inserted


async def test_prices_changed_for_costed_days_are_costed_again_from_that_day(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    """A contract entered with a start in the past reprices the days after it.

    The catch-up only looks past the newest cost, so those days kept the
    tariff prices they were first costed at, for good.
    """
    coord, yesterday, fetches, inserted = _costed_coordinator(monkeypatch, wire_recorder)
    changed = yesterday - datetime.timedelta(days=10)
    coord.tariff = _Tariff(changed_from=changed)

    await coord._async_inject_missing_costs(yesterday)

    assert fetches == [(changed, yesterday)]
    assert inserted == [(changed, yesterday)]
    assert coord._costs_repriced_from == changed


async def test_repriced_days_are_not_costed_again_on_the_next_refresh(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    coord, yesterday, fetches, _inserted = _costed_coordinator(monkeypatch, wire_recorder)
    coord.tariff = _Tariff(changed_from=yesterday - datetime.timedelta(days=10))

    await coord._async_inject_missing_costs(yesterday)
    await coord._async_inject_missing_costs(yesterday)

    assert len(fetches) == 1


async def test_unchanged_prices_fetch_nothing(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    coord, yesterday, fetches, _inserted = _costed_coordinator(monkeypatch, wire_recorder)

    await coord._async_inject_missing_costs(yesterday)

    assert fetches == []


async def test_costs_stored_before_fingerprints_are_taken_as_they_are(
    monkeypatch: pytest.MonkeyPatch, wire_recorder
) -> None:
    """An upgrade must not download the meter's whole history to check it.

    The prices those costs were computed at cannot be known; the tariff's
    are recorded for them, and a change made after that is caught.
    """
    coord, yesterday, fetches, _inserted = _costed_coordinator(monkeypatch, wire_recorder)
    coord._cost_prices_store.data = None

    await coord._async_inject_missing_costs(yesterday)

    assert fetches == []
    assert yesterday.isoformat() in coord._cost_prices_store.data["days"]

    changed = yesterday - datetime.timedelta(days=3)
    coord.tariff = _Tariff(changed_from=changed)
    await coord._async_inject_missing_costs(yesterday)

    assert fetches == [(changed, yesterday)]


async def test_a_refused_repricing_is_not_retried_on_every_refresh(
    monkeypatch: pytest.MonkeyPatch, wire_recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """A range the portal refuses is refused every time; the catch-up goes on."""
    coord, yesterday, fetches, _inserted = _costed_coordinator(
        monkeypatch, wire_recorder, fetch_raises=EneaApiError("refused")
    )
    coord.tariff = _Tariff(changed_from=yesterday - datetime.timedelta(days=10))

    await coord._async_inject_missing_costs(yesterday)
    await coord._async_inject_missing_costs(yesterday)

    assert len(fetches) == 1
    assert any("changed prices" in r.getMessage() for r in caplog.records)


def test_a_price_fingerprint_follows_the_price_and_skips_unpriced_days() -> None:
    day = datetime.date(2026, 3, 2)
    before = costs_module.price_signatures(_Tariff(), day, day)
    after = costs_module.price_signatures(_Tariff(changed_from=day), day, day)

    class _Unpriced(_Tariff):
        def get_period_for_date(self, day: datetime.date) -> None:
            return None

    assert before[day.isoformat()] != after[day.isoformat()]
    assert costs_module.price_signatures(_Unpriced(), day, day) == {}
