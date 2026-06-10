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

from .connector import EneaApiClient, EneaAuthError, EneaApiError, get_active_meter
from .const import (
    BACKFILL_MAX_CONSECUTIVE_EMPTY,
    BILL_KEY_CURRENT,
    BILL_KEY_PREVIOUS,
    DOMAIN,
    RANGE_FETCH_CHUNK_DAYS,
    STAT_KEY_ENERGY_CONSUMED,
    STAT_KEY_ENERGY_RETURNED,
    STAT_KEY_POWER_CONSUMED,
    STAT_KEY_POWER_RETURNED,
    MeasurementType,
    Resolution,
    STAT_NAME_BY_KEY,
)
from .billing import BillEstimate, async_estimate_bill, find_prices_config
from .costs import (
    async_get_cost_latest_date,
    async_insert_cost_statistics,
    find_tariff_group,
)
from .statistics import (
    async_insert_historical_statistics,
    get_statistic_id,
    has_data,
    slot_start_dt,
)

_LOGGER = logging.getLogger(__name__)


class EneaUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches meter data and injects historical statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EneaApiClient,
        meter_id: int,
        meter_code: str,
        update_interval: timedelta,
        fetch_consumption: bool = True,
        fetch_generation: bool = True,
        fetch_power_consumption: bool = False,
        fetch_power_generation: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client
        self.meter_id = meter_id
        self._meter_code = meter_code
        self._fetch_consumption = fetch_consumption
        self._fetch_generation = fetch_generation
        self._fetch_power_consumption = fetch_power_consumption
        self._fetch_power_generation = fetch_power_generation
        self._tariff_name: str | None = None
        self._assembly_datetime: datetime | None = None
        self._backfill_task: asyncio.Task[None] | None = None
        self.bill_prev_reading: date | None = None
        self.bill_last_reading: date | None = None
        self.bill_estimates: dict[str, BillEstimate | None] = {
            BILL_KEY_PREVIOUS: None,
            BILL_KEY_CURRENT: None,
        }

    def _get_measurement_types(self) -> list[tuple[str, MeasurementType]]:
        """Return active (key, measurement_type) pairs based on fetch settings."""
        candidates = [
            (self._fetch_consumption, STAT_KEY_ENERGY_CONSUMED, MeasurementType.ENERGY_CONSUMED),
            (self._fetch_generation, STAT_KEY_ENERGY_RETURNED, MeasurementType.ENERGY_RETURNED),
            (self._fetch_power_consumption, STAT_KEY_POWER_CONSUMED, MeasurementType.POWER_CONSUMED),
            (self._fetch_power_generation, STAT_KEY_POWER_RETURNED, MeasurementType.POWER_RETURNED),
        ]
        return [(key, mtype) for enabled, key, mtype in candidates if enabled]

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch meter data from the API."""
        try:
            data = await self.client.get_ppe_dashboard(self.meter_id)
        except EneaAuthError as err:
            raise ConfigEntryAuthFailed("Invalid credentials") from err
        except EneaApiError as err:
            raise UpdateFailed(f"Error fetching Enea data: {err}") from err

        self._tariff_name = data.get("tariffGroupName")

        # Determine assembly date of the currently active meter (no disassemblyDate).
        # Used as a lower bound when fetching statistics — avoids importing all-zero
        # data from the old meter for days after the new meter was installed.
        # Reset first so stale values don't persist if the field is absent.
        self._assembly_datetime = None
        active_meter = get_active_meter(data)
        if active_meter and active_meter.get("assemblyDate"):
            self._assembly_datetime = (
                dt_util.utc_from_timestamp(active_meter["assemblyDate"] / 1000)
                .astimezone(dt_util.DEFAULT_TIME_ZONE)
            )

        # Inject historical statistics — errors are non-fatal (dashboard data stays valid).
        try:
            await self._async_fetch_and_inject_stats()
        except Exception as err:
            _LOGGER.warning("Failed to update historical statistics: %s", err, exc_info=True)

        # Recompute bill estimates if reading dates are configured (new stats may have arrived).
        if self.bill_prev_reading is not None or self.bill_last_reading is not None:
            self.hass.async_create_task(self.async_recompute_bills())

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
        # All series are queried in parallel to avoid sequential executor round-trips.
        stat_ids = [
            get_statistic_id(self._meter_code, STAT_NAME_BY_KEY[key])
            for key, _ in keys_and_types
            if STAT_NAME_BY_KEY.get(key)
        ]
        last_stats_list = await asyncio.gather(*(
            get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, sid, True, {"sum", "mean"}
            )
            for sid in stat_ids
        ))

        latest_date: date | None = None
        for sid, last in zip(stat_ids, last_stats_list):
            if last.get(sid):
                ts = last[sid][0].get("start")
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
            # Incremental update: small range — run inline (fast).
            all_days = await self._fetch_days_forward(
                latest_date + timedelta(days=1), yesterday
            )
            if all_days:
                await self._async_inject_days(all_days)
                _LOGGER.debug("Injected statistics for %d day(s)", len(all_days))
            else:
                _LOGGER.debug(
                    "No new statistics available yet (last: %s, waiting for: %s)",
                    latest_date,
                    yesterday,
                )
                await self._async_inject_missing_costs(yesterday)
        else:
            # Initial backfill — potentially years of data (slow).
            # Schedule as a background task so the first coordinator refresh
            # returns quickly and sensors become available immediately.
            if self._backfill_task is None or self._backfill_task.done():
                self._backfill_task = self.hass.async_create_task(
                    self._async_do_initial_backfill(yesterday),
                    name=f"enea_backfill_{self._meter_code}",
                )

    async def _async_inject_days(
        self,
        all_days: list[tuple[date, dict[str, Any]]],
    ) -> None:
        """Inject energy statistics and, if a matching tariff exists, cost statistics.

        Cost statistics are external ("enea:..._koszt_...") and do not require any
        entities to be registered, so injection works during the first refresh or
        background backfill regardless of setup ordering.

        Args:
            all_days: Chronologically sorted list of (date, data_dict) tuples.
        """
        if not all_days:
            return
        await async_insert_historical_statistics(self.hass, self._meter_code, all_days)
        tariff = find_tariff_group(self.hass, self._tariff_name)
        if tariff is not None:
            await async_insert_cost_statistics(
                self.hass,
                self._meter_code,
                all_days,
                tariff,
                self._fetch_consumption,
                self._fetch_generation,
            )

    async def _async_do_initial_backfill(self, yesterday: date) -> None:
        """Fetch all historical data and inject statistics.

        Runs as a background task (started from _async_fetch_and_inject_stats)
        so that the first coordinator refresh does not block while years of data
        are being downloaded.
        """
        try:
            all_days = await self._fetch_days_backward(yesterday)
            if all_days:
                await self._async_inject_days(all_days)
                self.async_update_listeners()
                _LOGGER.debug(
                    "Initial backfill complete for meter %s: injected %d day(s)",
                    self._meter_code,
                    len(all_days),
                )
        except asyncio.CancelledError:
            _LOGGER.debug("Initial backfill cancelled for meter %s", self._meter_code)
            raise
        except Exception as err:
            _LOGGER.warning(
                "Initial backfill failed for meter %s: %s",
                self._meter_code,
                err,
                exc_info=True,
            )

    def cancel_backfill(self) -> None:
        """Cancel the background initial-backfill task if it is still running.

        Called on entry unload to prevent the task from running after the
        integration has been torn down.
        """
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
            _LOGGER.debug("Cancelled initial backfill for meter %s", self._meter_code)

    async def _async_inject_missing_costs(self, yesterday: date) -> None:
        """Inject cost statistics for days not yet covered, independently of energy.

        Called from _async_fetch_and_inject_stats when energy statistics are
        already up to date.  Checks the last cost stat date and fetches/injects
        any missing days.  Cost statistics are external, so this needs no
        entities and there is no separate post-setup pass.
        """
        if not (self._fetch_consumption or self._fetch_generation):
            # Costs are derived from energy data; nothing to do in power-only mode.
            return
        if self._backfill_task is not None and not self._backfill_task.done():
            # Initial backfill still running; it injects costs (energy + cost) itself.
            return
        tariff = find_tariff_group(self.hass, self._tariff_name)
        if tariff is None:
            return

        cost_latest = await async_get_cost_latest_date(
            self.hass, self._meter_code, tariff,
            self._fetch_consumption, self._fetch_generation,
        )
        if cost_latest is not None and cost_latest >= yesterday:
            _LOGGER.debug("Cost statistics already up to date (last: %s)", cost_latest)
            return

        if cost_latest is not None:
            start = cost_latest + timedelta(days=1)
        elif self._assembly_datetime is not None:
            # Start from assembly date — matches the lower bound used for energy stats.
            start = self._assembly_datetime.date()
        else:
            # No assembly date known (meter never replaced). Fall back to 365 days
            # to avoid unbounded API calls; a more precise start would require
            # querying the earliest energy statistic from the DB.
            start = yesterday - timedelta(days=364)

        days = await self._fetch_days_forward(start, yesterday)
        if days:
            await async_insert_cost_statistics(
                self.hass,
                self._meter_code,
                days,
                tariff,
                self._fetch_consumption,
                self._fetch_generation,
            )
            _LOGGER.debug("Injected cost statistics for %d day(s)", len(days))

    def _strip_pre_assembly_slots(
        self, day: date, day_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove hourly slots that predate the meter assembly on the assembly day.

        Keeps slots whose hour starts at or after the assembly hour, so the slot
        containing the assembly moment is included (it carries new-meter readings
        from the assembly minute onwards).  Compares each slot's actual start time
        (slot_start_dt, derived from integrationEnd) rather than timeId, so it
        stays correct even if the meter was assembled on a DST transition day.
        Example: assembly at 12:13 → keep slots starting 12:00 onwards.
        """
        if self._assembly_datetime is None or day != self._assembly_datetime.date():
            return day_data
        cutoff = self._assembly_datetime.replace(minute=0, second=0, microsecond=0)
        result: dict[str, Any] = {}
        for key, api_response in day_data.items():
            filtered_values = [
                entry for entry in api_response.get("values", [])
                if slot_start_dt(entry) >= cutoff
            ]
            result[key] = {**api_response, "values": filtered_values}
        return result

    @staticmethod
    def _split_range_response(
        api_response: dict[str, Any],
    ) -> dict[date, dict[str, Any]]:
        """Split a flat range API response into per-day response dicts.

        Groups the hourly slots by each slot's actual local date (derived from
        ``integrationEnd``) rather than by fixed 24-slot blocks, so DST transition
        days — which carry 23 or 25 slots, not 24 — are split correctly.  Each
        per-day dict has the same {'values', 'zones'} shape as a single-day
        response, so has_data, _collect_series and cost statistics need no changes.

        For very large ranges the API may return the per-hour data in
        'valuesToTable' instead of 'values', and can even leave a short partial
        slice in 'values'; whichever holds more entries is used so a partial
        'values' never shadows a complete 'valuesToTable'.

        Args:
            api_response: Raw API response from the range endpoint.

        Returns:
            Mapping of date → single-day response dict with 'values' and 'zones'.
        """
        values: list[dict[str, Any]] = api_response.get("values") or []
        values_to_table: list[dict[str, Any]] = api_response.get("valuesToTable") or []
        entries = values_to_table if len(values_to_table) > len(values) else values
        zones = api_response.get("zones", [])

        result: dict[date, dict[str, Any]] = {}
        for entry in entries:
            if entry.get("integrationEnd") is None:
                continue
            day = slot_start_dt(entry).date()
            result.setdefault(day, {"values": [], "zones": zones})["values"].append(entry)
        return result

    async def _fetch_range(
        self,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch all measurement types for a date range in parallel.

        Issues one request per active measurement type simultaneously, splits
        each flat response into per-day dicts, merges them, filters days with no
        data, and applies assembly-date slot stripping.

        Args:
            start_date: First date to fetch (inclusive); clamped to assembly date.
            end_date: Last date to fetch (inclusive).

        Returns:
            Chronologically sorted list of (date, day_data) tuples where day_data
            maps stat key → single-day API response dict.
        """
        keys_and_types = self._get_measurement_types()
        if not keys_and_types:
            return []

        if self._assembly_datetime is not None:
            start_date = max(start_date, self._assembly_datetime.date())

        if start_date > end_date:
            return []

        results = await asyncio.gather(
            *(
                self.client.get_consumption_data_range(
                    self.meter_id, start_date, end_date, mtype, Resolution.MIN_60
                )
                for _, mtype in keys_and_types
            ),
            return_exceptions=True,
        )

        # Split each measurement-type response into per-day dicts.
        per_key_days: dict[str, dict[date, dict[str, Any]]] = {}
        first_exc: BaseException | None = None
        for (key, mtype), result in zip(keys_and_types, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result  # propagate cancellation so shutdown/reload can proceed
                _LOGGER.warning(
                    "Range stats request failed for key=%s type=%d (%s–%s): %s",
                    key, mtype, start_date, end_date, result,
                    exc_info=True,
                )
                if first_exc is None:
                    first_exc = result
            else:
                per_key_days[key] = self._split_range_response(result)

        if not per_key_days:
            # All measurement-type requests failed — propagate to the caller.
            if first_exc is not None:
                raise first_exc
            raise EneaApiError("All range requests failed")

        # Merge: for each date present in any key, build unified day_data dict.
        all_dates: set[date] = set()
        for days_map in per_key_days.values():
            all_dates.update(days_map.keys())

        all_days: list[tuple[date, dict[str, Any]]] = []
        for day in sorted(all_dates):
            day_data: dict[str, Any] = {
                key: days_map[day]
                for key, days_map in per_key_days.items()
                if day in days_map
            }
            if not any(has_data(v) for v in day_data.values()):
                continue
            day_data = self._strip_pre_assembly_slots(day, day_data)
            all_days.append((day, day_data))

        return all_days

    async def _fetch_days_forward(
        self, start_date: date, end_date: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch days chronologically from start_date to end_date (inclusive).

        Splits the range into chunks of RANGE_FETCH_CHUNK_DAYS and fetches each
        chunk via _fetch_range (2-4 parallel requests per chunk).  Chunks are
        processed sequentially to avoid overloading the API.

        Skips days before the assembly date of the current meter.  On the assembly
        day itself, early-hour slots (before the assembly hour) are stripped by
        _strip_pre_assembly_slots so only new-meter data is imported.
        """
        # Assembly-date clamp is also applied inside _fetch_range; repeating it
        # here avoids allocating empty chunks before the assembly date.
        if self._assembly_datetime is not None:
            start_date = max(start_date, self._assembly_datetime.date())

        all_days: list[tuple[date, dict[str, Any]]] = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(
                chunk_start + timedelta(days=RANGE_FETCH_CHUNK_DAYS - 1),
                end_date,
            )
            chunk_days = await self._fetch_range(chunk_start, chunk_end)
            all_days.extend(chunk_days)
            chunk_start = chunk_end + timedelta(days=1)

        return all_days

    async def _fetch_days_backward(
        self, end_date: date
    ) -> list[tuple[date, dict[str, Any]]]:
        """Fetch days backward from end_date; stop when data runs out.

        When the assembly date is known, delegates to _fetch_days_forward because
        the exact start is known. When the assembly date is unknown, fetches
        RANGE_FETCH_CHUNK_DAYS-sized chunks going backward and stops after
        BACKFILL_MAX_CONSECUTIVE_EMPTY consecutive days with no data at the
        start (oldest end) of a chunk.

        Returns days in chronological (ascending) order.
        """
        if self._assembly_datetime is not None:
            _LOGGER.debug(
                "Assembly date known (%s) — fetching forward from assembly date",
                self._assembly_datetime.date(),
            )
            return await self._fetch_days_forward(self._assembly_datetime.date(), end_date)

        # Collect chunks newest-first, flatten in reverse at the end — avoids
        # O(n²) copies that would result from prepending to all_days each iteration.
        chunks: list[list[tuple[date, dict[str, Any]]]] = []
        chunk_end = end_date
        while True:
            chunk_start = chunk_end - timedelta(days=RANGE_FETCH_CHUNK_DAYS - 1)
            chunk_days = await self._fetch_range(chunk_start, chunk_end)

            if chunk_days:
                chunks.append(chunk_days)

                # Check if the oldest BACKFILL_MAX_CONSECUTIVE_EMPTY days of this
                # chunk were all empty — if so, data has run out.
                days_in_chunk = {d for d, _ in chunk_days}
                consecutive_empty = 0
                check_day = chunk_start
                while check_day <= chunk_end:
                    if check_day not in days_in_chunk:
                        consecutive_empty += 1
                        if consecutive_empty >= BACKFILL_MAX_CONSECUTIVE_EMPTY:
                            _LOGGER.debug(
                                "Stopping backfill: %d consecutive empty days "
                                "at start of chunk (reached %s)",
                                consecutive_empty,
                                chunk_start,
                            )
                            return [day for chunk in reversed(chunks) for day in chunk]
                    else:
                        break
                    check_day += timedelta(days=1)
            else:
                _LOGGER.debug(
                    "Stopping backfill: no data in chunk %s – %s",
                    chunk_start,
                    chunk_end,
                )
                break

            chunk_end = chunk_start - timedelta(days=1)

        return [day for chunk in reversed(chunks) for day in chunk]

    async def async_backfill(self, start_date: date, end_date: date) -> int:
        """Fetch and inject statistics for a custom date range.

        Returns the number of days for which data was found and injected.
        """
        all_days = await self._fetch_days_forward(start_date, end_date)
        if all_days:
            await self._async_inject_days(all_days)
            self.async_update_listeners()
            _LOGGER.info(
                "Backfill injected %d day(s) for meter %s (%s – %s)",
                len(all_days),
                self._meter_code,
                start_date,
                end_date,
            )
        return len(all_days)

    async def async_recompute_bills(self) -> None:
        """Recompute bill estimates for both reading periods and notify listeners.

        Previous period: (bill_prev_reading, bill_last_reading].
        Current period:  (bill_last_reading, yesterday].

        Sets the corresponding entry in bill_estimates to None when a period
        is empty, dates are missing, or statistics are unavailable.
        """
        cfg = find_prices_config(self.hass, self._tariff_name)
        if cfg is None:
            return

        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        d1 = self.bill_prev_reading
        d2 = self.bill_last_reading

        prev: BillEstimate | None = None
        if d1 is not None and d2 is not None and d1 < d2:
            try:
                prev = await async_estimate_bill(self.hass, self._meter_code, cfg, d1, d2)
            except Exception as err:
                _LOGGER.warning("Failed to estimate previous bill: %s", err, exc_info=True)

        current: BillEstimate | None = None
        if d2 is not None and d2 < yesterday:
            try:
                current = await async_estimate_bill(
                    self.hass, self._meter_code, cfg, d2, yesterday
                )
            except Exception as err:
                _LOGGER.warning("Failed to estimate current bill: %s", err, exc_info=True)

        self.bill_estimates[BILL_KEY_PREVIOUS] = prev
        self.bill_estimates[BILL_KEY_CURRENT] = current
        self.async_update_listeners()
