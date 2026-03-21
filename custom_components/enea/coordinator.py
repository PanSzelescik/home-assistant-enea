"""DataUpdateCoordinator for the Enea integration."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .connector import EneaApiClient, EneaAuthError, EneaApiError
from .const import (
    BACKFILL_DAYS_MAX,
    DOMAIN,
    STATS_ENERGY_CONSUMED,
    STATS_ENERGY_RETURNED,
    STATS_POWER_CONSUMED,
    STATS_POWER_RETURNED,
    STATS_RESOLUTION_60MIN,
)
from .statistics import async_insert_historical_statistics, get_statistic_id, has_data

_LOGGER = logging.getLogger(__name__)

# Maximum number of days to look back when BACKFILL_DAYS_MAX is set.
_MAX_BACKFILL_DAYS = 365
# Stop searching further back after this many consecutive days with no data.
_MAX_CONSECUTIVE_EMPTY = 7


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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch meter data from the API."""
        try:
            data = await self.connector.get_meter_data(self.meter_id)
        except EneaAuthError as err:
            raise ConfigEntryAuthFailed("Invalid credentials") from err
        except EneaApiError as err:
            raise UpdateFailed(f"Error fetching Enea data: {err}") from err

        # Inject historical statistics — errors are non-fatal (dashboard data stays valid).
        try:
            await self._async_fetch_and_inject_stats()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to update historical statistics: %s", err)

        return data

    # ------------------------------------------------------------------
    # Statistics helpers
    # ------------------------------------------------------------------

    async def _async_fetch_and_inject_stats(self) -> None:
        """Determine which days are missing and inject 15-minute statistics."""
        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        # Find the date of the most-recent already-injected statistic.
        statistic_id = get_statistic_id(self._meter_code, "Energia pobrana")
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        if last_stats.get(statistic_id):
            last_ts = last_stats[statistic_id][0].get("start")
            if last_ts is not None:
                last_date = (
                    dt_util.utc_from_timestamp(last_ts)
                    .astimezone(dt_util.DEFAULT_TIME_ZONE)
                    .date()
                )
                if last_date >= yesterday:
                    _LOGGER.debug("Statistics already up to date (last: %s)", last_date)
                    return
                # Fetch forward from the day after the last recorded entry.
                all_days = await self._fetch_days_forward(
                    last_date + timedelta(days=1), yesterday
                )
            else:
                all_days = await self._fetch_initial(yesterday)
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
        for _ in range(_MAX_BACKFILL_DAYS):
            day_data, any_data = await self._fetch_one_day(current)
            if any_data:
                all_days.append((current, day_data))
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY:
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
        """Fetch all 4 measurement types for a single day.

        Returns (day_data_dict, any_data_found).
        """
        date_str = day.isoformat()
        day_data: dict[str, Any] = {}
        any_data = False

        for key, mtype in (
            ("energy_consumed", STATS_ENERGY_CONSUMED),
            ("energy_returned", STATS_ENERGY_RETURNED),
            ("power_consumed", STATS_POWER_CONSUMED),
            ("power_returned", STATS_POWER_RETURNED),
        ):
            try:
                result = await self.connector.get_consumption_data(
                    self.meter_id, date_str, mtype, STATS_RESOLUTION_60MIN
                )
                day_data[key] = result
                if has_data(result):
                    any_data = True
            except EneaApiError as err:
                _LOGGER.debug(
                    "No stats data for %s type %d: %s", date_str, mtype, err
                )

        return day_data, any_data
