"""Fetch Linky measurements and publish their Energy statistics."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CoreState, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import APIError, AuthenticationError, HistoryLimitError, NoDataError, fetch_recent
from .const import API_REQUEST_DELAY, DEFAULT_SCAN_INTERVAL_HOURS, DOMAIN, PARIS_TZ
from .statistics import Buckets, async_write_statistics, daily_energy, hourly_energy, paris_midnight

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import LinkyClient, MeteringData

_LOGGER = logging.getLogger(__name__)
_POLL_DAYS = timedelta(days=7)
_CURVE_WINDOW = timedelta(days=7)
_MAX_IMPORT = timedelta(days=30)
_REQUEST_LOCK = f"{DOMAIN}_request_lock"
_ENERGY = {
    "daily_consumption": ("consumption", "daily"),
    "daily_production": ("production", "daily"),
    "load_curve": ("consumption", "hourly"),
    "production_load_curve": ("production", "hourly"),
}


@dataclass
class LinkyData:
    """Latest measurements for the Linky sensors."""

    daily_consumption: MeteringData | None = None
    load_curve: MeteringData | None = None
    max_power: MeteringData | None = None
    daily_production: MeteringData | None = None
    production_load_curve: MeteringData | None = None


class LinkyDataUpdateCoordinator(DataUpdateCoordinator[LinkyData]):
    """Refresh one meter and serialize historical imports with polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: LinkyClient,
        scan_interval_hours: int = DEFAULT_SCAN_INTERVAL_HOURS,
        *,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=scan_interval_hours),
            config_entry=config_entry,
        )
        self.client = client
        self._meter_lock = asyncio.Lock()
        self._request_lock = hass.data.setdefault(_REQUEST_LOCK, asyncio.Lock())

    async def _fetch(
        self,
        endpoint: Callable[..., Awaitable[MeteringData]],
        start: date,
        end: date,
        *,
        recent: bool = False,
    ) -> MeteringData | None:
        async with self._request_lock:
            # Space requests even when multiple meters refresh together.
            await asyncio.sleep(API_REQUEST_DELAY)
            try:
                if recent:
                    return await fetch_recent(endpoint, start, end)
                return await endpoint(start=start, end=end)
            except HistoryLimitError:
                if recent:
                    return None
                raise
            except NoDataError:
                return None
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed("Linky authentication failed") from err

    def _endpoints(self) -> dict[str, Callable[..., Awaitable[MeteringData]]]:
        return {
            "daily_consumption": self.client.get_daily_consumption,
            "daily_production": self.client.get_daily_production,
            "load_curve": self.client.get_consumption_load_curve,
            "production_load_curve": self.client.get_production_load_curve,
            "max_power": self.client.get_max_power,
        }

    async def _async_update_data(self) -> LinkyData:
        """Refetch the previous seven Paris days to include late revisions."""
        async with self._meter_lock:
            end = dt_util.now(PARIS_TZ).date()
            start = end - _POLL_DAYS
            data = LinkyData()
            series: dict[tuple[str, str], Buckets] = {}
            errors: list[str] = []
            endpoints = self._endpoints()
            for field, endpoint in endpoints.items():
                try:
                    result = await self._fetch(endpoint, start, end, recent=True)
                    if result is None or not result.interval_reading:
                        continue
                    if field in _ENERGY:
                        key = _ENERGY[field]
                        convert = daily_energy if key[1] == "daily" else hourly_energy
                        series[key] = convert(result, start, end)
                    setattr(data, field, result)
                except APIError as err:
                    errors.append(f"{field} ({err})")
                    if err.status_code == 429:
                        break
                except ValueError as err:
                    errors.append(f"{field} ({err})")

            if errors:
                if not any(getattr(data, field) is not None for field in endpoints):
                    raise UpdateFailed(f"Could not refresh Linky: {'; '.join(errors)}")
                _LOGGER.warning(
                    "Some Linky measurements could not be refreshed: %s", "; ".join(errors)
                )

            if any(series.values()):
                if self.hass.state is CoreState.running:
                    await self._async_write_refresh(series)
                else:
                    # Recorder does not drain its queue until Home Assistant has started.
                    async def write_after_start() -> None:
                        async with self._meter_lock:
                            await self._async_write_refresh(series)

                    @callback
                    def schedule_write(hass: HomeAssistant) -> None:
                        if (entry := self.config_entry) is not None:
                            entry.async_create_background_task(
                                hass, write_after_start(), "Write initial Linky statistics"
                            )
                        else:
                            hass.async_create_background_task(
                                write_after_start(), "Write initial Linky statistics"
                            )

                    cancel = async_at_started(self.hass, schedule_write)
                    if self.config_entry is not None:
                        self.config_entry.async_on_unload(cancel)
            return data

    async def _async_write_refresh(self, series: dict[tuple[str, str], Buckets]) -> None:
        try:
            await async_write_statistics(self.hass, self.client.prm, series)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not update Linky Energy statistics: %s", err)

    async def import_statistics(self, start: date, end: date) -> None:
        """Import a bounded date range and report any partial failure."""
        if not start < end <= dt_util.now(PARIS_TZ).date():
            raise ServiceValidationError(
                "Choose an increasing date range ending no later than today in Paris"
            )
        if end - start > _MAX_IMPORT:
            raise ServiceValidationError("Import at most 30 days at a time")

        if self.hass.state is not CoreState.running:
            raise HomeAssistantError("Wait for Home Assistant to finish starting before importing")

        async with self._meter_lock:
            endpoints = self._endpoints()
            series: dict[tuple[str, str], Buckets] = {}
            errors: list[str] = []
            rate_limited = False
            for field, key in _ENERGY.items():
                hourly = key[1] == "hourly"
                convert = hourly_energy if hourly else daily_energy
                measured: Buckets = {}
                saw_readings = False
                cursor = start
                while cursor < end:
                    limit = min(cursor + _CURVE_WINDOW, end) if hourly else end
                    try:
                        result = await self._fetch(endpoints[field], cursor, limit)
                        if result is not None and result.interval_reading:
                            saw_readings = True
                            measured.update(convert(result, cursor, limit))
                    except APIError as err:
                        errors.append(f"{field} {cursor} to {limit} ({err})")
                        if err.status_code == 429:
                            rate_limited = True
                            break
                    except ValueError as err:
                        errors.append(f"{field} {cursor} to {limit} ({err})")
                    cursor = limit

                if saw_readings:
                    expected = (
                        (paris_midnight(end) - paris_midnight(start)) // timedelta(hours=1)
                        if hourly
                        else (end - start).days
                    )
                    if len(measured) < expected:
                        count = expected - len(measured)
                        unit = "hours" if hourly else "days"
                        errors.append(f"{field} ({count} missing or invalid {unit})")
                series[key] = measured
                if rate_limited:
                    break

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
