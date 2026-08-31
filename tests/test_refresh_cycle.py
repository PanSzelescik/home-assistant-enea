"""What one refresh does about cost statistics that are behind."""
from __future__ import annotations

import datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea.connector import EneaApiError
from custom_components.enea.coordinator import EneaUpdateCoordinator


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
