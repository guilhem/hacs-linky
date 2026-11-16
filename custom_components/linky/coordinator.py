"""Fetch Linky measurements and publish their Energy statistics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from homeassistant.core import CoreState, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_utc_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import APIError, AuthenticationError, IntervalReading, MeteringData, NoDataError
from .budget import (
    DAILY_ATTEMPT_USED,
    ConsoBudgetError,
    ConsoBudgetExceeded,
    async_get_conso_budget,
)
from .const import DEFAULT_COLLECT_EXPORT, DOMAIN, PARIS_TZ
from .statistics import (
    Buckets,
    async_write_statistics,
    daily_energy,
    daily_from_hourly,
    hourly_energy,
    paris_midnight,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import LinkyClient

_LOGGER = logging.getLogger(__name__)
_MAX_WINDOW = timedelta(days=7)
_CONSO_FORBIDDEN_PENDING = f"{DOMAIN}_conso_forbidden_pending"


@dataclass
class LinkyData:
    """Latest measurements for the Linky sensors."""

    daily_consumption: MeteringData | None = None
    load_curve: MeteringData | None = None
    daily_production: MeteringData | None = None
    production_load_curve: MeteringData | None = None


class LinkyDataUpdateCoordinator(DataUpdateCoordinator[LinkyData | None]):
    """Refresh one meter and serialize historical imports with polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: LinkyClient,
        *,
        collect_export: bool = DEFAULT_COLLECT_EXPORT,
        config_entry: ConfigEntry | None = None,
        daily_only: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
            config_entry=config_entry,
            always_update=False,
        )
        self.client = client
        self._collect_export = collect_export
        self._batch_size = 2 if collect_export else 1
        self._daily_only = daily_only
        self._meter_lock = asyncio.Lock()
        self._cancel_deferred: Callable[[], None] | None = None
        self._skipped_update: tuple[bool, BaseException | None] | None = None
        self._manual_results: dict[asyncio.Task, dict[str, Any] | HomeAssistantError | None] = {}
        self._conso_forbidden_pending: WeakKeyDictionary[ConfigEntry, bool] = hass.data.setdefault(
            _CONSO_FORBIDDEN_PENDING, WeakKeyDictionary()
        )

    @callback
    def async_start(self) -> None:
        """Check for a daily slot each hour after the morning start time."""
        cancel = async_track_utc_time_change(
            self.hass,
            self._async_daily_slot,
            # An earlier batch can still occupy the rolling budget at the first slot.
            hour=range(7, 22),
            minute=30 + int(self.client.prm) % 30,
            second=0,
        )
        if self.config_entry is not None:
            self.config_entry.async_on_unload(cancel)
            self.config_entry.async_on_unload(self._async_cancel_deferred)

    @callback
    def _async_cancel_deferred(self) -> None:
        if self._cancel_deferred is not None:
            self._cancel_deferred()
            self._cancel_deferred = None

    def _defer_daily_slot(self, day: date, retry_in: float | None) -> None:
        if (
            retry_in is None
            or self._cancel_deferred is not None
            or self.config_entry is None
            or self.config_entry.pref_disable_polling
        ):
            return
        delay = max(1.0, retry_in)
        remaining = (paris_midnight(day + timedelta(days=1)) - dt_util.utcnow()).total_seconds()
        if delay >= remaining:
            return

        @callback
        def run_slot(when: datetime) -> None:
            self._cancel_deferred = None
            if dt_util.now(PARIS_TZ).date() == day:
                self._async_daily_slot(when)

        self._cancel_deferred = async_call_later(self.hass, delay, run_slot)

    @callback
    def _async_daily_slot(self, _when: datetime) -> None:
        if self.hass.state is not CoreState.running or self._cancel_deferred is not None:
            return
        entry = self.config_entry
        if entry is not None and entry.pref_disable_polling:
            return

        refresh = self.async_refresh()
        if entry is not None:
            entry.async_create_background_task(self.hass, refresh, "Refresh Linky daily data")
        else:
            self.hass.async_create_background_task(refresh, "Refresh Linky daily data")

    @callback
    def _async_update_conso_forbidden_issue(self, *, forbidden: bool, usable: bool) -> None:
        if (entry := self.config_entry) is None:
            return
        issue_id = f"conso_forbidden_{entry.entry_id}"
        if not forbidden:
            self._conso_forbidden_pending.pop(entry, None)
            if usable:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        elif self._conso_forbidden_pending.get(entry) is self._daily_only:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                config_entry_id=entry.entry_id,
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="conso_forbidden",
                translation_placeholders={"meter": self.client.prm[-4:]},
            )
        else:
            self._conso_forbidden_pending[entry] = self._daily_only

    async def _async_fetch_window(
        self, start: date, end: date, *, historical: bool = False
    ) -> tuple[LinkyData, dict[tuple[str, str], Buckets], list[str]]:
        data = LinkyData()
        series: dict[tuple[str, str], Buckets] = {}
        errors: list[str] = []
        endpoints = (
            (
                ("consumption", "daily_consumption", self.client.get_daily_consumption),
                ("production", "daily_production", self.client.get_daily_production),
            )
            if self._daily_only
            else (
                ("consumption", "load_curve", self.client.get_consumption_load_curve),
                ("production", "production_load_curve", self.client.get_production_load_curve),
            )
        )
        for direction, field, endpoint in endpoints if self._collect_export else endpoints[:1]:
            status = None
            usable = False
            try:
                result = await endpoint(start=start, end=end)
                if result is not None and result.interval_reading:
                    if self._daily_only:
                        totals = daily_energy(result, start, end)
                        measured = totals
                        expected = (end - start).days
                        unit = "days"
                    else:
                        measured = hourly_energy(result, start, end)
                        totals = daily_from_hourly(measured, start, end)
                        series[direction, "hourly"] = measured
                        setattr(data, field, result)
                        expected = (paris_midnight(end) - paris_midnight(start)) // timedelta(
                            hours=1
                        )
                        unit = "hours"

                    series[direction, "daily"] = totals
                    if totals:
                        if self._daily_only:
                            days = {midnight.astimezone(PARIS_TZ).date() for midnight in totals}
                            readings = [
                                reading
                                for reading in result.interval_reading
                                if reading.date in days
                            ]
                        else:
                            readings = [
                                IntervalReading(day.astimezone(PARIS_TZ).date(), kwh * 1000)
                                for day, kwh in sorted(totals.items())
                            ]
                        setattr(
                            data,
                            f"daily_{direction}",
                            replace(
                                result, start=start, end=end, interval_reading=readings, unit="Wh"
                            ),
                        )

                    usable = bool(measured)
                    if historical and (missing := expected - len(measured)) > 0:
                        errors.append(f"{field} ({missing} missing or invalid {unit})")
            except NoDataError:
                pass
            except AuthenticationError as err:
                if not historical and direction == "consumption":
                    self._async_update_conso_forbidden_issue(forbidden=False, usable=False)
                raise ConfigEntryAuthFailed("Linky authentication failed") from err
            except ConsoBudgetError as err:
                if not any(series.values()):
                    raise
                errors.append(f"{field} ({err})")
                break
            except APIError as err:
                status = err.status_code
                errors.append(f"{field} ({err})")
            except ValueError as err:
                errors.append(f"{field} ({err})")

            if not historical and direction == "consumption":
                self._async_update_conso_forbidden_issue(forbidden=status == 403, usable=usable)
            if status in (403, 429):
                break

        return data, series, errors

    async def async_refresh_now(self) -> dict[str, Any]:
        """Run the daily refresh and report the outcome of this invocation."""
        task = asyncio.current_task()
        assert task is not None
        self._manual_results[task] = {
            "status": "skipped",
            "reason": "The Linky coordinator is shutting down",
        }
        try:
            await self.async_refresh()
        finally:
            result = self._manual_results.pop(task)

        if isinstance(result, HomeAssistantError):
            raise result
        if result is not None:
            return result
        if isinstance(self.last_exception, ConfigEntryAuthFailed):
            raise HomeAssistantError("Linky authentication failed. Reauthenticate the meter.")
        if isinstance(self.last_exception, UpdateFailed):
            raise HomeAssistantError(
                f"{self.last_exception}. Check the Linky logs before the next daily refresh."
            )
        raise HomeAssistantError("The Linky refresh failed. Check the Home Assistant logs.")

    async def async_get_status(self) -> dict[str, Any]:
        """Read the local quota for a full daily refresh of this meter."""
        return await async_get_conso_budget(self.hass).async_get_status(
            self.client.prm, required=self._batch_size
        )

    def _set_manual_result(self, result: dict[str, Any] | HomeAssistantError | None) -> None:
        # A refresh only reports to the action that is awaiting it in the same task.
        if (task := asyncio.current_task()) in self._manual_results:
            self._manual_results[task] = result

    async def _async_update_data(self) -> LinkyData | None:
        """Refetch the previous seven Paris days to include late revisions."""
        async with self._meter_lock:
            end = dt_util.now(PARIS_TZ).date()
            self._skipped_update = None
            self._set_manual_result(None)
            if self.hass.state is not CoreState.running:
                return self._skip_update("Home Assistant is not running")
            budget = async_get_conso_budget(self.hass)
            try:
                async with budget.daily_batch(
                    self.client.prm, end, required=self._batch_size
                ) as claimed:
                    if not claimed:
                        return self._skip_update(DAILY_ATTEMPT_USED)
                    if dt_util.now(PARIS_TZ).date() != end:
                        return self._skip_update(
                            "The Paris date changed while the refresh was queued"
                        )
                    self._async_cancel_deferred()
                    data, series, errors = await self._async_fetch_window(end - _MAX_WINDOW, end)
            except ConsoBudgetExceeded as err:
                self._defer_daily_slot(end, err.retry_in)
                _LOGGER.debug("Deferring the Linky refresh: %s", err)
                return self._skip_update(str(err), err.retry_in)
            except ConsoBudgetError as err:
                raise UpdateFailed(str(err)) from err

            has_data = any(series.values())
            if errors:
                if not has_data:
                    raise UpdateFailed(f"Could not refresh Linky: {'; '.join(errors)}")
                _LOGGER.warning(
                    "Some Linky measurements could not be refreshed: %s", "; ".join(errors)
                )

            if has_data:
                try:
                    await async_write_statistics(self.hass, self.client.prm, series)
                except Exception as err:
                    self._set_manual_result(
                        HomeAssistantError(
                            "Could not confirm Linky Energy statistics in Recorder. "
                            "Check the Recorder logs before the next daily refresh."
                        )
                    )
                    if not isinstance(err, HomeAssistantError):
                        raise
                    _LOGGER.warning("Could not update Linky Energy statistics: %s", err)
                    return data

            if errors:
                self._set_manual_result({"status": "partial", "reason": "; ".join(errors)})
            else:
                self._set_manual_result({"status": "updated" if has_data else "no_data"})
            return data

    def _skip_update(self, reason: str, retry_in: float | None = None) -> LinkyData | None:
        result: dict[str, Any] = {"status": "skipped", "reason": reason}
        if retry_in is not None:
            result["retry_in_seconds"] = retry_in
        self._set_manual_result(result)
        if not self.last_update_success:
            # Keep the original failure and avoid HA logging a recovery on a skip.
            self._skipped_update = self.last_update_success, self.last_exception
            raise UpdateFailed("No Conso request was sent")
        return self.data

    @callback
    def _async_refresh_finished(self) -> None:
        if self._skipped_update is not None:
            self.last_update_success, self.last_exception = self._skipped_update
            self._skipped_update = None

    async def import_statistics(self, start: date, end: date) -> None:
        """Import one bounded date range and report any partial failure."""
        if not start < end <= dt_util.now(PARIS_TZ).date():
            raise ServiceValidationError(
                "Choose an increasing date range ending no later than today in Paris"
            )
        if end - start > _MAX_WINDOW:
            raise ServiceValidationError("Import at most 7 days at a time")

        if self.hass.state is not CoreState.running:
            raise HomeAssistantError("Wait for Home Assistant to finish starting before importing")

        async with self._meter_lock:
            _, series, errors = await self._async_fetch_window(start, end, historical=True)
            if not any(series.values()):
                if errors:
                    raise HomeAssistantError(f"Linky statistics import failed: {'; '.join(errors)}")
                raise ServiceValidationError(
                    "No Linky energy measurements are available for this range"
                )

            try:
                await async_write_statistics(self.hass, self.client.prm, series)
            except Exception as err:
                details = f"; provider issues: {'; '.join(errors)}" if errors else ""
                raise HomeAssistantError(
                    f"Could not complete the Linky Recorder import{details}"
                ) from err

            if errors:
                raise HomeAssistantError(
                    f"Linky statistics partially imported: {'; '.join(errors)}"
                )
