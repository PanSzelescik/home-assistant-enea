"""A running Home Assistant with the integration set up against saved responses.

conftest.py keeps most tests free of Home Assistant: they call the modules
directly.  Setting up a config entry, its platforms and its actions needs the
real thing, though — config entries, registries, entity platforms and
translations.  This builds it the way Home Assistant's bootstrap does, minus
the recorder: its long-term statistics live in StatisticsDatabase, an
in-memory stand-in the integration's modules are pointed at, and the Portal
Odbiorcy Enea is FakePortal, which answers from tests/fixtures/portal.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import bootstrap, config_entries, loader
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

import custom_components.enea as integration
from conftest import TEST_TIME_ZONE, load_portal_fixture
from custom_components.enea import billing, coordinator, costs, installation, statistics
from custom_components.enea.parsing import slot_start_dt

FAKE_METER_ID = 12345
FAKE_PPE = "590310600000001234"

SAVED_ON = date(2026, 10, 10)
"""The day the portal responses were saved; the portal had nothing for it yet."""

NOW = datetime.combine(SAVED_ON, time(9, 30), dt_util.get_default_time_zone())
"""When the tests run, as far as the integration can tell."""


class StatisticsDatabase:
    """Long-term statistics kept in memory, one series per statistic id.

    Answers the recorder calls the integration makes — get_last_statistics,
    statistics_during_period (hourly), get_metadata — and stores what
    async_add_external_statistics writes, replacing rows by start time as the
    recorder does.  It also stands in for the recorder instance, running
    executor jobs straight away.
    """

    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[float, dict[str, Any]]] = defaultdict(dict)

    def install(self, monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
        """Point every module of the integration that reaches the recorder at this.

        modules adds others, such as the real enea_prices' statistics module,
        which imports its statistics (async_import_statistics) the same way.
        """
        for module in (billing, coordinator, costs, installation, statistics, *modules):
            monkeypatch.setattr(module, "get_instance", lambda hass: self, raising=False)
            monkeypatch.setattr(module, "get_last_statistics", self.last, raising=False)
            monkeypatch.setattr(module, "statistics_during_period", self.during, raising=False)
            monkeypatch.setattr(module, "get_metadata", self.get_metadata, raising=False)
            monkeypatch.setattr(
                module, "async_add_external_statistics", self.add_external, raising=False
            )
            monkeypatch.setattr(module, "async_import_statistics", self.add_external, raising=False)

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        """Run a recorder query at once instead of in the recorder's thread."""
        return target(*args)

    async def async_block_till_done(self) -> None:
        """Writes apply at once, so there is never anything to wait for."""

    def add_external(self, hass: Any, metadata: dict[str, Any], rows: list[Any]) -> None:
        """Store rows the way async_add_external_statistics does."""
        statistic_id = metadata["statistic_id"]
        self.metadata[statistic_id] = dict(metadata)
        for row in rows:
            self.rows[statistic_id][row["start"].timestamp()] = {
                key: value for key, value in row.items() if key != "start"
            }

    def series(self, statistic_id: str) -> list[dict[str, Any]]:
        """Return a statistic's rows oldest first, each with its start timestamp."""
        return [
            {"start": start, **row} for start, row in sorted(self.rows.get(statistic_id, {}).items())
        ]

    def last(
        self, hass: Any, number: int, statistic_id: str, convert: bool, types: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Stand in for get_last_statistics: the newest rows, newest first."""
        rows = self.series(statistic_id)[-number:][::-1]
        return {statistic_id: rows} if rows else {}

    def during(
        self,
        hass: Any,
        start: datetime,
        end: datetime | None = None,
        statistic_ids: set[str] | None = None,
        period: str = "hour",
        units: Any = None,
        types: set[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Stand in for statistics_during_period, by hour or by local day.

        'change' is the difference of a row's sum from the sum before it,
        reaching back before the window as the recorder does.  A day row
        carries the sum and state of the day's last hour, as the recorder's
        daily statistics do.
        """
        assert period in ("hour", "day"), period
        found: dict[str, list[dict[str, Any]]] = {}
        for statistic_id in statistic_ids or []:
            series = self.series(statistic_id)
            if period == "day":
                days: dict[float, dict[str, Any]] = {}
                for row in series:
                    local = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
                    days[dt_util.start_of_local_day(local.date()).timestamp()] = row
                series = [{**row, "start": start} for start, row in days.items()]
            previous = 0.0
            rows = []
            for row in series:
                inside = row["start"] >= start.timestamp() and (
                    end is None or row["start"] < end.timestamp()
                )
                if inside:
                    rows.append({**row, "change": (row.get("sum") or 0.0) - previous})
                previous = row.get("sum") or 0.0
            if rows:
                found[statistic_id] = rows
        return found

    def get_metadata(self, hass: Any, **kwargs: Any) -> dict[str, tuple[int, dict[str, Any]]]:
        """Stand in for get_metadata: every statistic stored, keyed by id."""
        source = kwargs.get("statistic_source")
        return {
            statistic_id: (index, meta)
            for index, (statistic_id, meta) in enumerate(sorted(self.metadata.items()))
            if source is None or meta.get("source") == source
        }


RANGE_FIXTURES = {
    1: ["range_2026-03-01_2026-10-10_1", "range_2026-03-27_2026-04-02_1"],
    4: ["range_2026-03-27_2026-04-02_4"],
    5: ["range_2026-03-27_2026-04-02_5"],
    9: ["range_2026-03-27_2026-04-02_9"],
}
"""Saved range responses by measurement type, the widest first."""


class FakePortal:
    """The Portal Odbiorcy Enea of the fixture account, answering from saved responses.

    A range request is answered with the saved hours of the requested days;
    days no saved response covers come back as nothing at all, as a range
    before a meter's history does.  Every request is recorded.
    """

    session_closed = False

    def __init__(self) -> None:
        self.dashboard = load_portal_fixture("dashboard")
        # Dashboards of other meters on the account, by meter id; any other id
        # answers with the fixture meter's.
        self.dashboards: dict[int, dict[str, Any]] = {}
        self.ppes = load_portal_fixture("user_ppes")
        self.ranges = {
            mtype: [load_portal_fixture(name) for name in names]
            for mtype, names in RANGE_FIXTURES.items()
        }
        self.requests: list[tuple[Any, ...]] = []
        self.fail_with: Exception | None = None
        # Only the range requests fail with this; the dashboard still answers.
        self.fail_ranges_with: Exception | None = None
        # Range requests wait for this event when it is set, to catch a refresh
        # or an unload while the background import is under way.
        self.gate: asyncio.Event | None = None
        # Report the meter as a prosumer's (type 2), as the portal does after a
        # PV installation is connected.
        self.prosumer = False
        # The portal's messages; the fixture account has one, read long ago.
        self.notifications: list[dict[str, Any]] = load_portal_fixture("notifications")
        # Only the message requests fail with this; the dashboard still answers.
        self.fail_notifications_with: Exception | None = None
        # Only marking a message read fails with this.
        self.fail_read_with: Exception | None = None

    def _check(self, *request: Any) -> None:
        """Record a request and raise the failure a test asked for, if any."""
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with

    async def authenticate(self) -> None:
        """Log in; always succeeds unless a failure was set."""
        self._check("authenticate")

    def update_credentials(self, password: str) -> None:
        """Accept a new password."""

    async def get_meters(self) -> list[dict[str, Any]]:
        """Return /user/ppes."""
        self._check("ppes")
        return [{**ppe, "type": 2 if self.prosumer else ppe["type"]} for ppe in load_portal_fixture("user_ppes")]

    async def get_ppe_dashboard(self, meter_id: int) -> dict[str, Any]:
        """Return /consumptionDashboard/ppe/{id}."""
        self._check("dashboard", meter_id)
        dashboard = copy.deepcopy(self.dashboards.get(meter_id, self.dashboard))
        return {**dashboard, "type": 2} if self.prosumer else dashboard

    async def get_consumption_data_range(
        self,
        meter_id: int,
        first: date,
        last: date,
        mtype: int,
        resolution: int,
        data_source: int | None = None,
    ) -> dict[str, Any]:
        """Return the saved hours of first..last for one measurement type."""
        self._check("range", meter_id, first, last, int(mtype), data_source)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_ranges_with is not None:
            raise self.fail_ranges_with
        for response in self.ranges.get(int(mtype), []):
            hours = [
                entry for entry in response["valuesToTable"]
                if first <= slot_start_dt(entry).date() <= last
            ]
            if hours:
                return {"values": hours, "valuesToTable": hours, "zones": response["zones"]}
        return {"values": [], "valuesToTable": [], "zones": []}


    async def get_notifications(
        self, meter_ids: list[int], first: date | None = None, last: date | None = None
    ) -> list[dict[str, Any]]:
        """Return the messages received first..last, every one without dates."""
        self._check("notifications", tuple(meter_ids), first, last)
        if self.fail_notifications_with is not None:
            raise self.fail_notifications_with
        return [
            copy.deepcopy(message) for message in self.notifications
            if first is None
            or first <= dt_util.as_local(dt_util.utc_from_timestamp(message["date"] / 1000)).date() <= last
        ]

    async def mark_notification_read(self, notification_id: int) -> None:
        """Mark a message read, as closing its window in the portal does."""
        self._check("read", notification_id)
        if self.fail_read_with is not None:
            raise self.fail_read_with
        for message in self.notifications:
            if message["id"] == notification_id:
                message["isRead"] = True


class FakeZonePricing:
    """Netto per-kWh prices of one zone, as enea_prices' ZonePricing exposes them."""

    def __init__(self, energy: float, variable_network: float) -> None:
        self.energy = energy
        self.variable_network = variable_network
        self.quality = 0.03
        self.oze = 0.007
        self.cogeneration = 0.003
        self.total_distribution = variable_network + self.quality + self.oze + self.cogeneration


class FakeMonthlyFees:
    """Fixed monthly fees, as enea_prices' MonthlyFees exposes them."""

    def get_network_fixed(self, phases: int) -> float:
        """Return the fixed network fee: three phases cost more."""
        return 12.0 if phases == 3 else 8.0

    def get_capacity(self, annual_kwh: int) -> float:
        """Return the capacity fee for the yearly consumption bracket."""
        return 4.0 if annual_kwh < 1200 else 10.0

    def get_subscription(self, billing_months: int) -> float:
        """Return the subscription fee: a longer billing period costs less a month."""
        return 0.5 if billing_months > 1 else 1.0

    def get_transition(self, annual_kwh: int) -> float:
        """Return the transition fee: abolished, as in 2026."""
        return 0.0


class FakeG12Period:
    """A G12 price period with Enea's schedule: night 22–6 and 13–15."""

    NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5, 13, 14})

    def __init__(self, valid_from: date, valid_until: date) -> None:
        self.valid_from = valid_from
        self.valid_until = valid_until
        self.zones = {"day": FakeZonePricing(0.6, 0.3), "night": FakeZonePricing(0.4, 0.1)}
        self.monthly = FakeMonthlyFees()

    def get_zone_at_hour(self, hour: int, day: date | None = None) -> str:
        """Return the zone of an hour; the day does not matter in G12."""
        return "night" if hour in self.NIGHT_HOURS else "day"


class FakeG12Tariff:
    """The G12 tariff group of enea_prices, priced over one period."""

    name = "G12"

    def __init__(self, valid_from: date = date(2026, 1, 1), valid_until: date = date(2026, 12, 31)) -> None:
        self.periods = [FakeG12Period(valid_from, valid_until)]

    def get_period_for_date(self, day: date) -> FakeG12Period | None:
        """Return the period covering the day, None outside the table."""
        return next(
            (p for p in self.periods if p.valid_from <= day <= p.valid_until), None
        )


class FakeG11Period(FakeG12Period):
    """A G11 price period: one zone for every hour."""

    def __init__(self, valid_from: date, valid_until: date) -> None:
        super().__init__(valid_from, valid_until)
        self.zones = {"day": FakeZonePricing(0.5, 0.25)}

    def get_zone_at_hour(self, hour: int, day: date | None = None) -> str:
        """Every hour is in the only zone."""
        return "day"


class FakeG11Tariff(FakeG12Tariff):
    """The G11 tariff group of enea_prices, priced over one period."""

    name = "G11"

    def __init__(self, valid_from: date = date(2026, 1, 1), valid_until: date = date(2026, 12, 31)) -> None:
        self.periods = [FakeG11Period(valid_from, valid_until)]


def make_prices_entry(tariff: FakeG12Tariff | None = None) -> config_entries.ConfigEntry:
    """Return a loaded-looking enea_prices entry for the tariff's group (G12 by default).

    Only the parts the integration reaches for duck-typed are there: the
    entry's data and its runtime_data.
    """
    tariff = tariff or FakeG12Tariff()
    entry = config_entries.ConfigEntry(
        data={"tariff": tariff.name, "phases": 3, "annual_kwh": 2000, "billing_months": 2},
        discovery_keys=MappingProxyType({}),
        domain="enea_prices",
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title=f"Ceny {tariff.name}",
        unique_id=tariff.name,
        version=1,
    )
    entry.runtime_data = SimpleNamespace(
        tariff=tariff, phases=3, annual_kwh=2000, billing_months=2
    )
    return entry


def register_prices_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    """Make an enea_prices entry visible to async_entries without setting it up.

    enea_prices is not installed in the tests; the integration only looks the
    entry up and reads its runtime_data, so it is put straight into the
    registry the way a loaded entry sits there.
    """
    hass.config_entries._entries[entry.entry_id] = entry


async def async_start_hass(config_dir: str) -> HomeAssistant:
    """Return a running Home Assistant with its registries and config entries loaded.

    The recorder is marked as set up rather than started — the integration's
    statistics go to StatisticsDatabase.
    """
    hass = HomeAssistant(config_dir)
    await hass.config.async_set_time_zone(TEST_TIME_ZONE)
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    hass.config.components.add("recorder")
    # The real start: what waits for it — the restore-state dump among them — runs.
    await hass.async_start()
    assert hass.state is CoreState.running
    return hass


def make_entry(
    meter_name: str = FAKE_PPE, meter_id: int = FAKE_METER_ID, **options: Any
) -> config_entries.ConfigEntry:
    """Return a config entry for a meter of the fixture account.

    Every meter answers with the fixture meter's data; meter_name tells the
    entries of a second meter on the same account apart.
    """
    return config_entries.ConfigEntry(
        data={
            "username": "test@example.invalid",
            "password": "test-password",
            "meter_id": meter_id,
            "meter_name": meter_name,
            "tariff": "G12",
        },
        discovery_keys=MappingProxyType({}),
        domain="enea",
        minor_version=1,
        options={
            "update_interval": {"hours": 3, "minutes": 30},
            "fetch_consumption": True,
            "fetch_generation": True,
            "fetch_power_consumption": False,
            "fetch_power_generation": False,
            **options,
        },
        source="user",
        subentries_data=None,
        title=f"Enea {meter_name}",
        unique_id=meter_name,
        version=1,
    )


def at_local(day: date, hour: int = 0) -> datetime:
    """Return a local hour as an aware datetime."""
    return datetime.combine(day, time(hour), dt_util.get_default_time_zone())


def days_between(first: date, last: date) -> list[date]:
    """Return every day from first to last, inclusive."""
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


# --- fixtures, imported by the test modules that use them ---------------------


class Clock:
    """The time the integration sees; tests move it on between refreshes."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self, time_zone: Any = None) -> datetime:
        """Stand in for dt_util.now."""
        return self.now.astimezone(time_zone or dt_util.get_default_time_zone())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Fix the clock at the moment the portal responses were saved."""
    current = Clock()
    monkeypatch.setattr(dt_util, "now", current)
    return current


@pytest.fixture
def portal(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> FakePortal:
    """Serve the fixture account instead of the Portal Odbiorcy Enea.

    Its responses were saved on SAVED_ON, so the clock is pinned with it: a
    test running the integration on the real date would ask for days the
    fixtures do not have, and pass or fail with the calendar.
    """
    fake = FakePortal()
    monkeypatch.setattr(integration, "EneaApiClient", lambda *args: fake)
    monkeypatch.setattr(integration, "async_create_clientsession", lambda hass: None)
    return fake


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> StatisticsDatabase:
    """Keep the long-term statistics in memory."""
    store = StatisticsDatabase()
    store.install(monkeypatch)
    return store


@pytest.fixture
async def hass(tmp_path, clock, portal, database):
    """A running Home Assistant; stopped, with its tasks, after the test."""
    instance = await async_start_hass(str(tmp_path))
    yield instance
    await instance.async_stop(force=True)


FAKE_INTEGRATIONS = Path(__file__).parent / "fake_integrations"
"""Holds custom_components/enea_prices, a stand-in for the real integration."""


@pytest.fixture
def enea_prices_installed(monkeypatch: pytest.MonkeyPatch):
    """Make the stand-in enea_prices a custom integration Home Assistant can find.

    Its modules are dropped afterwards, so no other test sees its AKCYZA or
    tariff table.
    """
    monkeypatch.syspath_prepend(str(FAKE_INTEGRATIONS))
    yield
    for name in [name for name in sys.modules if name.startswith("custom_components.enea_prices")]:
        del sys.modules[name]


def find_enea_prices_repository() -> Path | None:
    """Return a checkout of home-assistant-enea-prices, None when there is none.

    ENEA_PRICES_PATH names it (CI checks it out there); otherwise a checkout
    next to this repository is used.
    """
    configured = os.environ.get("ENEA_PRICES_PATH")
    candidate = (
        Path(configured) if configured
        else Path(__file__).resolve().parents[2] / "home-assistant-enea-prices"
    )
    if (candidate / "custom_components" / "enea_prices" / "manifest.json").is_file():
        return candidate
    return None


@pytest.fixture
def real_enea_prices(monkeypatch: pytest.MonkeyPatch, database: StatisticsDatabase):
    """Make the real enea_prices a custom integration Home Assistant can find.

    Its price statistics go to the same in-memory database.  Skipped when no
    checkout is found, failed when ENEA_PRICES_PATH names one that is not
    there — CI sets it, so the contract is never skipped there.
    """
    repository = find_enea_prices_repository()
    if repository is None:
        if os.environ.get("ENEA_PRICES_PATH"):
            pytest.fail(f"ENEA_PRICES_PATH={os.environ['ENEA_PRICES_PATH']} has no enea_prices")
        pytest.skip("enea_prices not found: set ENEA_PRICES_PATH or check it out next to this repository")
    pytest.importorskip("holidays")
    monkeypatch.syspath_prepend(str(repository))
    package = importlib.import_module("custom_components.enea_prices")
    database.install(monkeypatch, importlib.import_module("custom_components.enea_prices.statistics"))
    yield package
    for name in [name for name in sys.modules if name.startswith("custom_components.enea_prices")]:
        del sys.modules[name]
