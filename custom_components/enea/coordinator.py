"""DataUpdateCoordinator for the Enea integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.helpers.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient, EneaAuthError, EneaApiError
from .const import (
    BACKFILL_DAYS_MAX,
    BACKFILL_MAX_CONSECUTIVE_EMPTY,
    DOMAIN,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    STAT_KEY_POWER_CONSUMED,
    STAT_KEY_POWER_RETURNED,
    STAT_NAME_BY_KEY,
    STATS_ENERGY_CONSUMED,
    STATS_ENERGY_RETURNED,
    STATS_POWER_CONSUMED,
    STATS_POWER_RETURNED,
    STATS_RESOLUTION_60MIN,
)
from .costs import (
    async_insert_cost_statistics,
    find_tariff_group,
    get_cost_stats,
)
from .statistics import async_insert_historical_statistics, get_statistic_id, has_data

_LOGGER = logging.getLogger(__name__)


class EneaUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches meter data and injects historical statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        connector: EneaApiClient,
        meter_id: int,
        meter_code: str,
        backfill_days: int,
        update_interval: timedelta,
        fetch_consumption: bool = True,
        fetch_generation: bool = True,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.connector = connector
        self.meter_id = meter_id
        self._meter_code = meter_code
        self._backfill_days = backfill_days
        self._fetch_consumption = fetch_consumption
        self._fetch_generation = fetch_generation
        self._tariff_name: str | None = None
        self._assembly_date: date | None = None
        self._assembly_datetime: datetime | None = None
        self.cost_sums: dict[str, float] = {}
        self._pending_cost_days: list[tuple[date, dict[str, Any]]] = []

    def _get_measurement_types(self) -> list[tuple[str, int]]:
        """Return active (key, measurement_type) pairs based on fetch settings."""
        types: list[tuple[str, int]] = []
        if self._fetch_consumption:
            types.extend([
                (STAT_KEY_ENERGY_CONSUMED, STATS_ENERGY_CONSUMED),
                (STAT_KEY_POWER_CONSUMED, STATS_POWER_CONSUMED),
            ])
        if self._fetch_generation:
            types.extend([
                (STAT_KEY_ENERGY_RETURNED, STATS_ENERGY_RETURNED),
                (STAT_KEY_POWER_RETURNED, STATS_POWER_RETURNED),
            ])
        return types

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch meter data from the API."""
        try:
            data = await self.connector.get_ppe_dashboard(self.meter_id)
        except EneaAuthError as err:
            raise ConfigEntryAuthFailed("Invalid credentials") from err
        except EneaApiError as err:
            raise UpdateFailed(f"Error fetching Enea data: {err}") from err

        self._tariff_name = data.get("tariffGroupName")

        # Determine assembly date of the currently active meter (no disassemblyDate).
        # Used as a lower bound when fetching statistics — avoids importing all-zero
        # data from the old meter for days after the new meter was installed.
        active_meter = next(
            (m for m in data.get("meters", []) if m.get("disassemblyDate") is None),
            None,
        )
        if active_meter and active_meter.get("assemblyDate"):
            self._assembly_datetime = (
                dt_util.utc_from_timestamp(active_meter["assemblyDate"] / 1000)
                .astimezone(dt_util.DEFAULT_TIME_ZONE)
            )
            self._assembly_date = self._assembly_datetime.date()

        # Inject historical statistics — errors are non-fatal (dashboard data stays valid).
        try:
            await self._async_fetch_and_inject_stats()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to update historical statistics: %s", err, exc_info=True)

        return data

    # ------------------------------------------------------------------
    # Statistics helpers
    # ------------------------------------------------------------------

    async def _async_fetch_and_inject_stats(self) -> None:
        """Determine which days are missing and inject historical statistics."""
        keys_and_types = self._get_measurement_types()
        if not keys_and_types:
            return

        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        # Find the most-recent date across all active statistic series.
        latest_date: date | None = None
        for key, _ in keys_and_types:
            name = STAT_NAME_BY_KEY.get(key)
            if name is None:
                continue
            stat_id = get_statistic_id(self._meter_code, name)
            last = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, stat_id, True, {"sum", "mean"}
            )
            if last.get(stat_id):
                ts = last[stat_id][0].get("start")
                if ts is not None:
                    d = (
                        dt_util.utc_from_timestamp(ts)
                        .astimezone(dt_util.DEFAULT_TIME_ZONE)
                        .date()
                    )
                    if d >= yesterday:
                        _LOGGER.debug("Statistics already up to date (last: %s)", d)
                        # Energy is current — check costs independently.
                        await self._async_inject_missing_costs(yesterday)
                        return
                    if latest_date is None or d > latest_date:
                        latest_date = d

        if latest_date is not None:
            all_days = await self._fetch_days_forward(
                latest_date + timedelta(days=1), yesterday
            )
        else:
            all_days = await self._fetch_initial(yesterday)

        if all_days:
            await async_insert_historical_statistics(
                self.hass, self._meter_code, all_days
            )
            _LOGGER.debug("Injected statistics for %d day(s)", len(all_days))

            tariff = find_tariff_group(self.hass, self._tariff_name)
            if tariff is not None:
                sums = await async_insert_cost_statistics(
                    self.hass,
                    self._meter_code,
                    all_days,
                    tariff,
                    self._fetch_consumption,
                    self._fetch_generation,
                )
                if sums:
                    self.cost_sums.update(sums)
                else:
                    # Cost sensor entities not yet registered (setup still in
                    # progress); save days for async_setup_costs() called later.
                    self._pending_cost_days = all_days

    async def async_setup_costs(self) -> None:
        """Inject cost statistics after sensor entities have been registered.

        Called from async_setup_entry after async_forward_entry_setups so that
        EneaCostSensor entities exist in the entity registry.  Handles two cases:

        - Pending days saved during first refresh (energy newly fetched but cost
          injection failed because entities were not yet registered).
        - Energy already up to date (e.g. reload triggered by enea_prices install).
        """
        yesterday = dt_util.now().date() - timedelta(days=1)
        if self._pending_cost_days:
            days = self._pending_cost_days
            self._pending_cost_days = []
            tariff = find_tariff_group(self.hass, self._tariff_name)
            if tariff is not None:
                sums = await async_insert_cost_statistics(
                    self.hass,
                    self._meter_code,
                    days,
                    tariff,
                    self._fetch_consumption,
                    self._fetch_generation,
                )
                self.cost_sums.update(sums)
        else:
            await self._async_inject_missing_costs(yesterday)

    async def _async_inject_missing_costs(self, yesterday: date) -> None:
        """Inject cost statistics for days not yet covered, independently of energy.

        Called when energy statistics are already up to date.  Checks the last
        cost stat date and fetches/injects any missing days.  Also populates
        coordinator.cost_sums from the DB when everything is already current.
        """
        tariff = find_tariff_group(self.hass, self._tariff_name)
        if tariff is None:
            return

        cost_latest, existing_sums = await get_cost_stats(self.hass, self._meter_code)
        if cost_latest is not None and cost_latest >= yesterday:
            _LOGGER.debug("Cost statistics already up to date (last: %s)", cost_latest)
            if not self.cost_sums:
                self.cost_sums.update(existing_sums)
            return

        if cost_latest is not None:
            start = cost_latest + timedelta(days=1)
        elif self._assembly_date is not None:
            # Start from assembly date — matches the lower bound used for energy stats.
            start = self._assembly_date
        elif self._backfill_days == BACKFILL_DAYS_MAX:
            start = yesterday - timedelta(days=364)
        else:
            start = yesterday - timedelta(days=max(self._backfill_days - 1, 0))

        days = await self._fetch_days_forward(start, yesterday)
        if days:
            sums = await async_insert_cost_statistics(
                self.hass,
                self._meter_code,
                days,
                tariff,
                self._fetch_consumption,
                self._fetch_generation,
            )
            self.cost_sums.update(sums)
            _LOGGER.debug("Injected cost statistics for %d day(s)", len(days))

    async def _fetch_initial(
        self, yesterday: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch the initial batch of days (no existing statistics in DB)."""
        if self._backfill_days == BACKFILL_DAYS_MAX:
            # Search backwards until data runs out.
            return await self._fetch_days_backward(yesterday)
        start_date = yesterday - timedelta(days=max(self._backfill_days - 1, 0))
        return await self._fetch_days_forward(start_date, yesterday)

    def _strip_pre_assembly_slots(
        self, day: date, day_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove hourly slots that predate the meter assembly on the assembly day.

        timeId N covers the hour (N-1):00–N:00.  We keep timeId > HH, where HH
        is the assembly hour, so the slot that contains the assembly moment is
        included (it carries new-meter readings from the assembly minute onwards).
        Example: assembly at 12:13 → cutoff=12 → keep timeId > 12 (13+, i.e. 12:00 onwards).
        """
        if self._assembly_datetime is None or day != self._assembly_date:
            return day_data
        cutoff_time_id = self._assembly_datetime.hour
        result: dict[str, Any] = {}
        for key, api_response in day_data.items():
            filtered_values = [
                entry for entry in api_response.get("values", [])
                if entry.get("timeId", 0) > cutoff_time_id
            ]
            result[key] = {**api_response, "values": filtered_values}
        return result

    async def _fetch_days_forward(
        self, start_date: date, end_date: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch days chronologically from start_date to end_date (inclusive).

        Skips days before the assembly date of the current meter.  On the assembly
        day itself, early-hour slots (before the assembly hour) are stripped by
        _strip_pre_assembly_slots so only new-meter data is imported.
        """
        if self._assembly_date is not None:
            start_date = max(start_date, self._assembly_date)
        all_days: list[tuple[date, dict[str, Any]]] = []
        current = start_date
        while current <= end_date:
            day_data, any_data = await self._fetch_one_day(current)
            if any_data:
                day_data = self._strip_pre_assembly_slots(current, day_data)
                all_days.append((current, day_data))
            current += timedelta(days=1)
        return all_days

    async def _fetch_days_backward(
        self, end_date: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch days backward from end_date; stop after consecutive empty days.

        Returns days in chronological (ascending) order.
        """
        all_days: list[tuple[date, dict[str, Any]]] = []
        consecutive_empty = 0
        current = end_date
        while True:
            if self._assembly_date is not None and current < self._assembly_date:
                _LOGGER.debug(
                    "Stopping backfill at assembly date %s", self._assembly_date
                )
                break
            day_data, any_data = await self._fetch_one_day(current)
            if any_data:
                day_data = self._strip_pre_assembly_slots(current, day_data)
                all_days.append((current, day_data))
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= BACKFILL_MAX_CONSECUTIVE_EMPTY:
                    _LOGGER.debug(
                        "Stopping backfill after %d consecutive empty days (reached %s)",
                        consecutive_empty,
                        current,
                    )
                    break
            current -= timedelta(days=1)
        return list(reversed(all_days))

    async def _fetch_one_day(
        self, day: date
    ) -> tuple[dict[str, Any], bool]:
        """Fetch active measurement types for a single day in parallel.

        Returns (day_data_dict, any_data_found).
        """
        keys_and_types = self._get_measurement_types()
        if not keys_and_types:
            return {}, False

        date_str = day.isoformat()
        results = await asyncio.gather(
            *(
                self.connector.get_consumption_data(
                    self.meter_id, date_str, mtype, STATS_RESOLUTION_60MIN
                )
                for _, mtype in keys_and_types
            ),
            return_exceptions=True,
        )

        day_data: dict[str, Any] = {}
        any_data = False
        for (key, mtype), result in zip(keys_and_types, results):
            if isinstance(result, BaseException):
                _LOGGER.debug(
                    "No stats data for %s type %d: %s", date_str, mtype, result,
                    exc_info=result,
                )
                continue
            day_data[key] = result
            if has_data(result):
                any_data = True

        return day_data, any_data

    async def async_backfill(self, start_date: date, end_date: date) -> int:
        """Fetch and inject statistics for a custom date range.

        Returns the number of days for which data was found and injected.
        """
        all_days = await self._fetch_days_forward(start_date, end_date)
        if all_days:
            await async_insert_historical_statistics(
                self.hass, self._meter_code, all_days
            )
            tariff = find_tariff_group(self.hass, self._tariff_name)
            if tariff is not None:
                sums = await async_insert_cost_statistics(
                    self.hass,
                    self._meter_code,
                    all_days,
                    tariff,
                    self._fetch_consumption,
                    self._fetch_generation,
                )
                self.cost_sums.update(sums)
            _LOGGER.info(
                "Backfill injected %d day(s) for meter %s (%s – %s)",
                len(all_days),
                self._meter_code,
                start_date,
                end_date,
            )
        return len(all_days)
