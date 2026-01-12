"""DataUpdateCoordinator for Linky integration."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from pylinky import APIError, AsyncLinkyClient, AuthenticationError, MeteringData

from .const import API_REQUEST_DELAY, CONF_PRICE_ENTITY, DOMAIN

# Paris timezone for Linky data
PARIS_TZ = dt_util.get_time_zone("Europe/Paris")

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class LinkyData:
    """Data class to hold all Linky data."""

    daily_consumption: MeteringData | None = None
    load_curve: MeteringData | None = None
    max_power: MeteringData | None = None
    daily_production: MeteringData | None = None
    production_load_curve: MeteringData | None = None


class LinkyDataUpdateCoordinator(DataUpdateCoordinator[LinkyData]):
    """Class to manage fetching Linky data."""

    config_entry: ConfigEntry

    # Default fetch period for first run or when no previous data exists
    DEFAULT_FETCH_DAYS = 3

    def __init__(
        self,
        hass: HomeAssistant,
        client: AsyncLinkyClient,
        scan_interval_hours: int = 6,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=scan_interval_hours),
        )
        self.client = client
        # Track last known data dates to optimize API calls
        self._last_consumption_date: date | None = None
        self._last_load_curve_date: date | None = None
        self._last_production_date: date | None = None

    def _get_fetch_start_date(self, last_date: date | None) -> date:
        """Calculate optimal start date for API fetch.

        If we have previous data, start from the day after the last known date.
        Otherwise, use the default fetch period.
        We always fetch at least 1 day back from last_date to catch late updates.
        """
        today = date.today()

        if last_date is None:
            # First fetch: use default period
            return today - timedelta(days=self.DEFAULT_FETCH_DAYS)

        # Start from last known date (re-fetch it in case of late updates)
        start = last_date

        # But don't go back more than DEFAULT_FETCH_DAYS
        min_start = today - timedelta(days=self.DEFAULT_FETCH_DAYS)
        if start < min_start:
            start = min_start

        return start

    def _update_last_dates(self, data: LinkyData) -> None:
        """Update tracked last dates from fetched data."""
        if data.daily_consumption and data.daily_consumption.interval_reading:
            readings = data.daily_consumption.interval_reading
            last_reading = max(readings, key=lambda r: r.date)
            last_date = last_reading.date
            if isinstance(last_date, datetime):
                last_date = last_date.date()
            self._last_consumption_date = last_date

        if data.load_curve and data.load_curve.interval_reading:
            readings = data.load_curve.interval_reading
            last_reading = max(readings, key=lambda r: r.date)
            last_date = last_reading.date
            if isinstance(last_date, datetime):
                last_date = last_date.date()
            self._last_load_curve_date = last_date

        if data.daily_production and data.daily_production.interval_reading:
            readings = data.daily_production.interval_reading
            last_reading = max(readings, key=lambda r: r.date)
            last_date = last_reading.date
            if isinstance(last_date, datetime):
                last_date = last_date.date()
            self._last_production_date = last_date

    async def _async_update_data(self) -> LinkyData:
        """Fetch data from Linky API."""
        data = LinkyData()

        # Calculate optimal date range based on what we already have
        # Use the oldest of our tracked dates as start to ensure we get all data types
        end = date.today()
        start = self._get_fetch_start_date(
            min(
                filter(
                    None,
                    [
                        self._last_consumption_date,
                        self._last_load_curve_date,
                        self._last_production_date,
                    ],
                ),
                default=None,
            )
        )

        _LOGGER.debug(
            (
                "Fetching data from %s to %s (last known dates: "
                "consumption=%s, load_curve=%s, production=%s)"
            ),
            start,
            end,
            self._last_consumption_date,
            self._last_load_curve_date,
            self._last_production_date,
        )

        try:
            # Fetch consumption data with delays between requests
            # to respect API rate limits (max 5 req/sec)
            try:
                data.daily_consumption = await self.client.get_daily_consumption(
                    start=start, end=end
                )
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch daily consumption: %s", err)

            await asyncio.sleep(API_REQUEST_DELAY)

            try:
                data.load_curve = await self.client.get_consumption_load_curve(start=start, end=end)
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch load curve: %s", err)

            await asyncio.sleep(API_REQUEST_DELAY)

            try:
                data.max_power = await self.client.get_max_power(start=start, end=end)
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch max power: %s", err)

            await asyncio.sleep(API_REQUEST_DELAY)

            # Fetch production data (may fail if user has no solar panels)
            try:
                data.daily_production = await self.client.get_daily_production(start=start, end=end)
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch daily production: %s", err)

            await asyncio.sleep(API_REQUEST_DELAY)

            try:
                data.production_load_curve = await self.client.get_production_load_curve(
                    start=start, end=end
                )
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch production load curve: %s", err)

        except AuthenticationError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except (OSError, TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        # If we got no data at all, something is wrong
        if data.daily_consumption is None and data.load_curve is None and data.max_power is None:
            raise UpdateFailed("Failed to fetch any consumption data from API")

        # Insert hourly statistics from load curve data for granular history
        try:
            await self._insert_hourly_statistics(
                load_curve=data.load_curve,
                production_load_curve=data.production_load_curve,
            )
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # Recorder may not be available (e.g., in tests or if disabled)
            _LOGGER.debug("Failed to insert hourly statistics: %s", err)

        # Update tracked dates for next fetch optimization
        self._update_last_dates(data)

        return data

    def _get_price_info(self) -> tuple[float, str] | None:
        """Get price value and currency from the configured price entity.

        Returns a tuple of (price, currency) or None if not configured or unavailable.
        """
        price_entity_id = self.config_entry.options.get(CONF_PRICE_ENTITY)
        if not price_entity_id:
            return None

        price_state = self.hass.states.get(price_entity_id)
        if price_state is None:
            _LOGGER.debug("Price entity %s not found", price_entity_id)
            return None

        if price_state.state in (None, "unknown", "unavailable"):
            _LOGGER.debug("Price entity %s is unavailable", price_entity_id)
            return None

        try:
            price = float(price_state.state)
        except ValueError:
            _LOGGER.warning(
                "Price entity %s has invalid value: %s",
                price_entity_id,
                price_state.state,
            )
            return None

        # Get currency from unit_of_measurement (e.g., "EUR/kWh", "USD/kWh")
        unit = price_state.attributes.get("unit_of_measurement", "")
        # Extract currency from unit like "EUR/kWh" -> "EUR"
        currency = (
            unit.split("/")[0].strip() if "/" in unit else unit if unit else "EUR"
        )

        return (price, currency)

    async def _insert_hourly_statistics(
        self,
        load_curve: MeteringData | None = None,
        production_load_curve: MeteringData | None = None,
    ) -> None:
        """Insert hourly statistics from load curve data.

        The load curve provides 30-minute interval data in Watts (average power).
        We convert this to Wh and aggregate to hourly statistics for the Energy dashboard.
        """
        if load_curve is None and production_load_curve is None:
            return

        prm = self.client.prm

        # Get price info if configured
        price_info = self._get_price_info()

        # Define statistic IDs for hourly data (kWh)
        consumption_hourly_id = f"{DOMAIN}:{prm}_energy_consumption_hourly_kwh"
        production_hourly_id = f"{DOMAIN}:{prm}_energy_production_hourly_kwh"

        _LOGGER.debug(
            "Updating hourly statistics for consumption: %s and production: %s",
            consumption_hourly_id,
            production_hourly_id,
        )

        # Process consumption load curve (with cost if price is configured)
        if load_curve and load_curve.interval_reading:
            cost_id = f"{DOMAIN}:{prm}_energy_consumption_hourly_cost"
            await self._process_hourly_load_curve(
                load_curve=load_curve,
                statistic_id=consumption_hourly_id,
                name=f"Linky {prm} hourly consumption",
                price_info=price_info,
                cost_statistic_id=cost_id,
                cost_name=f"Linky {prm} hourly consumption cost",
            )

        # Process production load curve (no cost for production)
        if production_load_curve and production_load_curve.interval_reading:
            await self._process_hourly_load_curve(
                load_curve=production_load_curve,
                statistic_id=production_hourly_id,
                name=f"Linky {prm} hourly production",
            )

    async def _process_hourly_load_curve(
        self,
        load_curve: MeteringData,
        statistic_id: str,
        name: str,
        price_info: tuple[float, str] | None = None,
        cost_statistic_id: str | None = None,
        cost_name: str | None = None,
    ) -> None:
        """Process load curve data and insert hourly statistics.

        The load curve provides 30-minute interval data in Watts (average power).
        The timestamp represents the END of the interval.
        So "2024-01-01 00:30:00" means data from 00:00 to 00:30.

        We convert W to Wh (power * 0.5h) and aggregate to hourly statistics.
        If price_info is provided, also creates cost statistics.
        """
        # Metadata for hourly statistics (kWh)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        # Get last statistics to determine starting point
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )

        if not last_stat:
            energy_sum = 0.0
            last_stats_time = None
        else:
            row = last_stat.get(statistic_id, [{}])[0]
            last_stats_time = row.get("start")  # type: ignore[assignment]
            energy_sum = float(row.get("sum") or 0)

        # If we have price info, also get last cost statistics
        cost_sum = 0.0
        if price_info and cost_statistic_id:
            last_cost_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, cost_statistic_id, True, set()
            )
            if last_cost_stat:
                cost_row = last_cost_stat.get(cost_statistic_id, [{}])[0]
                cost_sum = float(cost_row.get("sum") or 0)

        # Aggregate 30-min readings to hourly
        # Group readings by the hour they belong to
        # The timestamp is the END of the interval, so:
        # - 00:30 belongs to hour 00:00 (interval 00:00-00:30)
        # - 01:00 belongs to hour 00:00 (interval 00:30-01:00)
        # - 01:30 belongs to hour 01:00 (interval 01:00-01:30)
        hourly_energy: dict[datetime, float] = defaultdict(float)

        # Sort readings by date to ensure correct processing order
        sorted_readings = sorted(load_curve.interval_reading, key=lambda r: r.date)

        for reading in sorted_readings:
            reading_dt = reading.date
            if not isinstance(reading_dt, datetime):
                # Skip if we don't have time information (shouldn't happen for load curve)
                _LOGGER.debug("Skipping reading without time info: %s", reading_dt)
                continue

            # The timestamp is the END of the 30-min interval
            # Get the START of the interval by subtracting 30 minutes
            interval_start = reading_dt - timedelta(minutes=30)

            # Get the hour this interval belongs to (truncate to hour)
            # Use Paris timezone for the hour calculation
            if interval_start.tzinfo is None:
                interval_start = interval_start.replace(tzinfo=PARIS_TZ)

            hour_start = interval_start.replace(minute=0, second=0, microsecond=0)

            # Convert power (W) to energy (Wh): W * 0.5h = Wh
            energy_wh = reading.value * 0.5
            hourly_energy[hour_start] += energy_wh / 1000.0  # store in kWh

        # Create statistics from hourly aggregated data
        statistics = []
        cost_statistics = []
        price = price_info[0] if price_info else 0.0

        for hour_start in sorted(hourly_energy.keys()):
            stat_time = dt_util.as_utc(hour_start)

            # Skip if we already have this statistic
            if last_stats_time and stat_time.timestamp() <= last_stats_time:
                continue

            energy_kwh = hourly_energy[hour_start]
            energy_sum += energy_kwh

            statistics.append(
                StatisticData(
                    start=stat_time,
                    state=energy_kwh,
                    sum=energy_sum,
                )
            )

            # Calculate cost if price info is available
            if price_info and cost_statistic_id:
                cost = energy_kwh * price
                cost_sum += cost
                cost_statistics.append(
                    StatisticData(
                        start=stat_time,
                        state=cost,
                        sum=cost_sum,
                    )
                )

        # Add energy statistics to Home Assistant
        if statistics:
            _LOGGER.debug(
                "Adding %s hourly statistics for %s",
                len(statistics),
                statistic_id,
            )
            async_add_external_statistics(self.hass, metadata, statistics)

        # Add cost statistics if available
        if cost_statistics and price_info and cost_statistic_id and cost_name:
            currency = price_info[1]
            cost_metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=cost_name,
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_class=None,
                unit_of_measurement=currency,
            )
            _LOGGER.debug(
                "Adding %s hourly cost statistics for %s (currency: %s)",
                len(cost_statistics),
                cost_statistic_id,
                currency,
            )
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

    async def import_statistics(self, start: date, end: date) -> None:
        """Import statistics for a custom date range."""
        _LOGGER.info("Starting import of statistics from %s to %s", start, end)

        prm = self.client.prm

        # Define statistic IDs (kWh variants for Energy Dashboard pricing compatibility)
        consumption_statistic_id = f"{DOMAIN}:{prm}_energy_consumption_kwh"
        production_statistic_id = f"{DOMAIN}:{prm}_energy_production_kwh"

        # Metadata for consumption statistics (kWh)
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        # Metadata for production statistics (kWh)
        production_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} production",
            source=DOMAIN,
            statistic_id=production_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        # Get last statistics to calculate proper sum
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )

        # Get the sum at the start date or initialize
        if last_stat:
            cons_row = last_stat.get(consumption_statistic_id, [{}])[0]
            consumption_sum = float(cons_row.get("sum") or 0)
        else:
            consumption_sum = 0.0

        last_prod_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, production_statistic_id, True, set()
        )
        if last_prod_stat:
            prod_row = last_prod_stat.get(production_statistic_id, [{}])[0]
            production_sum = float(prod_row.get("sum") or 0)
        else:
            production_sum = 0.0

        _LOGGER.debug("Fetching consumption data from %s to %s", start, end)

        # Fetch consumption data
        consumption_statistics = []
        try:
            daily_data = await self.client.get_daily_consumption(start=start, end=end)
            if daily_data and daily_data.interval_reading:
                for reading in daily_data.interval_reading:
                    reading_date = reading.date
                    # Convert date to datetime at midnight UTC
                    stat_time = datetime.combine(reading_date, datetime.min.time())
                    stat_time = dt_util.as_utc(stat_time)

                    # Convert Wh -> kWh
                    consumption_state = float(reading.value) / 1000.0
                    consumption_sum += consumption_state

                    consumption_statistics.append(
                        StatisticData(
                            start=stat_time,
                            state=consumption_state,
                            sum=consumption_sum,
                        )
                    )

                _LOGGER.debug("Fetched %s consumption data points", len(consumption_statistics))
        except AuthenticationError:
            raise
        except APIError as err:
            _LOGGER.error("Failed to fetch consumption data for import: %s", err)

        # Fetch production data
        production_statistics = []
        try:
            production_data = await self.client.get_daily_production(start=start, end=end)
            if production_data and production_data.interval_reading:
                for reading in production_data.interval_reading:
                    reading_date = reading.date
                    # Convert date to datetime at midnight UTC
                    stat_time = datetime.combine(reading_date, datetime.min.time())
                    stat_time = dt_util.as_utc(stat_time)

                    # Convert Wh -> kWh
                    production_state = float(reading.value) / 1000.0
                    production_sum += production_state

                    production_statistics.append(
                        StatisticData(
                            start=stat_time,
                            state=production_state,
                            sum=production_sum,
                        )
                    )

                _LOGGER.debug("Fetched %s production data points", len(production_statistics))
        except AuthenticationError:
            raise
        except APIError as err:
            _LOGGER.debug("Failed to fetch production data for import: %s", err)

        # Add statistics to Home Assistant
        if consumption_statistics:
            _LOGGER.info(
                "Importing %s consumption statistics",
                len(consumption_statistics),
            )
            async_add_external_statistics(self.hass, consumption_metadata, consumption_statistics)

        if production_statistics:
            _LOGGER.info(
                "Importing %s production statistics",
                len(production_statistics),
            )
            async_add_external_statistics(self.hass, production_metadata, production_statistics)

        # Also import hourly statistics from load curve
        await self._import_hourly_statistics(start=start, end=end)

    async def _import_hourly_statistics(self, start: date, end: date) -> None:
        """Import hourly statistics from load curve data for a date range."""
        prm = self.client.prm

        # Fetch and process consumption load curve (kWh)
        try:
            await asyncio.sleep(API_REQUEST_DELAY)
            load_curve = await self.client.get_consumption_load_curve(start=start, end=end)
            if load_curve and load_curve.interval_reading:
                await self._process_hourly_load_curve(
                    load_curve=load_curve,
                    statistic_id=f"{DOMAIN}:{prm}_energy_consumption_hourly_kwh",
                    name=f"Linky {prm} hourly consumption",
                )
                _LOGGER.info(
                    "Imported hourly consumption statistics from load curve (%s readings)",
                    len(load_curve.interval_reading),
                )
        except AuthenticationError:
            raise
        except APIError as err:
            _LOGGER.debug("Failed to fetch consumption load curve for import: %s", err)

        # Fetch and process production load curve (kWh)
        try:
            await asyncio.sleep(API_REQUEST_DELAY)
            production_load_curve = await self.client.get_production_load_curve(
                start=start, end=end
            )
            if production_load_curve and production_load_curve.interval_reading:
                await self._process_hourly_load_curve(
                    load_curve=production_load_curve,
                    statistic_id=f"{DOMAIN}:{prm}_energy_production_hourly_kwh",
                    name=f"Linky {prm} hourly production",
                )
                _LOGGER.info(
                    "Imported hourly production statistics from load curve (%s readings)",
                    len(production_load_curve.interval_reading),
                )
        except AuthenticationError:
            raise
        except APIError as err:
            _LOGGER.debug("Failed to fetch production load curve for import: %s", err)
