"""DataUpdateCoordinator for the Enea integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
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

        # Inject historical statistics — errors are non-fatal (dashboard data stays valid).
        try:
            await self._async_fetch_and_inject_stats()
        except Exception as err:
            _LOGGER.warning("Failed to update historical statistics: %s", err)

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

    async def _fetch_initial(
        self, yesterday: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch the initial batch of days (no existing statistics in DB)."""
        if self._backfill_days == BACKFILL_DAYS_MAX:
            # Search backwards until data runs out.
            return await self._fetch_days_backward(yesterday)
        start_date = yesterday - timedelta(days=max(self._backfill_days - 1, 0))
        return await self._fetch_days_forward(start_date, yesterday)

    async def _fetch_days_forward(
        self, start_date: date, end_date: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch days chronologically from start_date to end_date (inclusive)."""
        all_days: list[tuple[date, dict[str, Any]]] = []
        current = start_date
        while current <= end_date:
            day_data, any_data = await self._fetch_one_day(current)
            if any_data:
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
            day_data, any_data = await self._fetch_one_day(current)
            if any_data:
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
                    "No stats data for %s type %d: %s", date_str, mtype, result
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
            _LOGGER.info(
                "Backfill injected %d day(s) for meter %s (%s – %s)",
                len(all_days),
                self._meter_code,
                start_date,
                end_date,
            )
        return len(all_days)
