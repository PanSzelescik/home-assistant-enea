"""Errors of the statistics step are logged with their traceback, without the customer.

Logging with exc_info prints the traceback as it is.  A recorder error quotes
the statistic_id, which is the PPE number, and an aiohttp one quotes the URL
with the portal's meter id — and the log ends up in a public GitHub issue.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

from custom_components.enea import coordinator as coordinator_module
from custom_components.enea.connector import masked_traceback
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    FAKE_METER_ID,
    FAKE_PPE,
    NOW,
    clock,
    database,
    hass,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
)

LEAK = (
    f"UNIQUE constraint failed for enea:{FAKE_PPE}_energia_pobrana, asked "
    f"https://portal.invalid/api/consumption/{FAKE_METER_ID}/2026-01-01/2026-06-30/1/2"
)


def _raise_leak(*args: Any, **kwargs: Any) -> None:
    """Fail the way the recorder or aiohttp does, quoting the PPE and the meter id."""
    raise RuntimeError(LEAK)


def _own_log(caplog: pytest.LogCaptureFixture) -> str:
    """Return what the integration logged; Home Assistant logs entity ids with the PPE itself."""
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.enea")
    )


def _assert_masked(text: str) -> None:
    """The text has the traceback and the error, with both identifiers hidden."""
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: UNIQUE constraint failed for enea:…1234_energia_pobrana" in text
    assert "/api/consumption/…/2026-01-01/2026-06-30/1/2" in text
    assert FAKE_PPE not in text
    assert f"/{FAKE_METER_ID}/" not in text


def test_masked_traceback_keeps_the_frames_and_hides_the_customer():
    """The traceback names where the error came from; the identifiers are gone."""
    try:
        _raise_leak()
    except RuntimeError as err:
        text = masked_traceback(err)

    _assert_masked(text)
    assert "_raise_leak" in text
    assert not text.endswith("\n")


def test_masked_traceback_covers_the_chained_error():
    """An error raised from another one is printed with it, masked as well."""
    try:
        try:
            _raise_leak()
        except RuntimeError as err:
            raise ValueError(f"while writing {FAKE_PPE}") from err
    except ValueError as err:
        text = masked_traceback(err)

    _assert_masked(text)
    assert "ValueError: while writing …1234" in text


async def test_failed_import_is_logged_without_the_customer(hass, caplog, monkeypatch):
    """The import in the background fails: one warning, traceback masked."""
    monkeypatch.setattr(coordinator_module, "async_insert_historical_statistics", _raise_leak)
    caplog.set_level(logging.DEBUG)

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done(wait_background_tasks=True)

    [record] = [r for r in caplog.records if r.getMessage().startswith("Initial backfill failed")]
    assert record.exc_info is None
    _assert_masked(record.getMessage())
    assert FAKE_PPE not in _own_log(caplog)
    state = entry.runtime_data.coordinator.diagnostics_state()
    assert FAKE_PPE not in state["initial_backfill"]


async def test_failed_refresh_is_logged_without_the_customer(hass, caplog, clock, monkeypatch):
    """A refresh whose statistics step fails logs the masked traceback and carries on."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data.coordinator
    monkeypatch.setattr(coordinator_module, "async_insert_historical_statistics", _raise_leak)
    clock.now = NOW + timedelta(days=3)
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    [record] = [
        r for r in caplog.records
        if r.getMessage().startswith("Failed to update historical statistics")
    ]
    assert record.exc_info is None
    _assert_masked(record.getMessage())
    assert FAKE_PPE not in _own_log(caplog)
    assert FAKE_PPE not in coordinator.diagnostics_state()["statistics_error"]


@pytest.mark.parametrize("target,message", [
    pytest.param("_async_latest_statistics_date", "Failed to read the latest statistics date", id="date"),
    pytest.param("async_detect_installation", "Failed to work out the installation", id="installation"),
    pytest.param("async_update_prices_setup_issue", "Failed to check the enea_prices setup", id="prices"),
])
async def test_every_step_after_the_import_logs_masked(hass, caplog, monkeypatch, target, message):
    """Each part of the statistics step that only logs its failure masks it."""
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data.coordinator
    if hasattr(coordinator_module, target):
        monkeypatch.setattr(coordinator_module, target, _raise_leak)
    else:
        monkeypatch.setattr(coordinator, target, _raise_leak)
    caplog.clear()

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    [record] = [r for r in caplog.records if r.getMessage().startswith(message)]
    assert record.exc_info is None
    _assert_masked(record.getMessage())
    assert FAKE_PPE not in _own_log(caplog)


async def test_failed_bill_estimate_logs_masked(hass, caplog, monkeypatch):
    """An estimate that fails is logged the same way, and leaves the sensor empty."""
    register_prices_entry(hass, make_prices_entry())
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data.coordinator
    coordinator.bill_prev_reading = NOW.date() - timedelta(days=60)
    coordinator.bill_last_reading = NOW.date() - timedelta(days=30)
    monkeypatch.setattr(coordinator_module, "async_estimate_bill", _raise_leak)
    caplog.clear()

    await coordinator.async_recompute_bills()

    messages = [r.getMessage() for r in caplog.records if "Failed to estimate" in r.getMessage()]
    assert [message.split(":")[0] for message in messages] == [
        "Failed to estimate previous bill", "Failed to estimate current bill",
    ]
    for message in messages:
        _assert_masked(message)
    assert FAKE_PPE not in _own_log(caplog)
    assert set(coordinator.bill_estimates.values()) == {None}
