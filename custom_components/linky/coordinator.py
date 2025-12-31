"""DataUpdateCoordinator for Linky integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from pylinky import APIError, AsyncLinkyClient, AuthenticationError, MeteringData

from .const import API_REQUEST_DELAY, DEFAULT_SCAN_INTERVAL, DOMAIN

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

    def __init__(
        self,
        hass: HomeAssistant,
        client: AsyncLinkyClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> LinkyData:
        """Fetch data from Linky API."""
        data = LinkyData()

        # Fetch data for the last 7 days to get recent consumption for sensors
        # The API returns data up to yesterday typically
        end = date.today()
        start = end - timedelta(days=7)

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
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        # If we got no data at all, something is wrong
        if data.daily_consumption is None and data.load_curve is None and data.max_power is None:
            raise UpdateFailed("Failed to fetch any consumption data from API")

        # Insert statistics using the data we already fetched (no extra API calls)
        try:
            await self._insert_statistics(
                daily_consumption=data.daily_consumption,
                daily_production=data.daily_production,
            )
        except KeyError:
            # Recorder not available (e.g., in tests or if disabled)
            _LOGGER.debug("Recorder not available, skipping statistics insertion")

        return data

    async def _insert_statistics(
        self,
        daily_consumption: MeteringData | None = None,
        daily_production: MeteringData | None = None,
    ) -> None:
        """Insert Linky statistics for daily consumption and production.

        Uses data already fetched by _async_update_data to avoid duplicate API calls.
        """
        prm = self.client.prm

        # Define statistic IDs
        consumption_statistic_id = f"{DOMAIN}:{prm}_energy_consumption"
        production_statistic_id = f"{DOMAIN}:{prm}_energy_production"

        _LOGGER.debug(
            "Updating statistics for consumption: %s and production: %s",
            consumption_statistic_id,
            production_statistic_id,
        )

        # Metadata for consumption statistics
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        )

        # Metadata for production statistics
        production_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} production",
            source=DOMAIN,
            statistic_id=production_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        )

        # Get last statistics to determine starting point
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )

        # Determine if this is first time or incremental update
        if not last_stat:
            _LOGGER.debug("Updating statistics for the first time")
            consumption_sum = 0.0
            production_sum = 0.0
            last_stats_time = None
        else:
            # Get info about last statistic
            last_stats_time = last_stat[consumption_statistic_id][0]["start"]

            # Get current sum from last statistic
            consumption_sum = float(last_stat[consumption_statistic_id][0].get("sum", 0))

            # Get production sum if exists
            last_prod_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, production_statistic_id, True, set()
            )
            production_sum = (
                float(last_prod_stat[production_statistic_id][0].get("sum", 0))
                if last_prod_stat
                else 0.0
            )

        # Process consumption data (already fetched by _async_update_data)
        consumption_statistics = []
        if daily_consumption and daily_consumption.interval_reading:
            for reading in daily_consumption.interval_reading:
                reading_date = reading.date
                # Convert date to datetime at midnight UTC
                stat_time = datetime.combine(reading_date, datetime.min.time())
                stat_time = dt_util.as_utc(stat_time)

                # Skip if we already have this statistic
                if last_stats_time and stat_time.timestamp() <= last_stats_time:
                    continue

                # Value in Wh
                consumption_state = float(reading.value)
                consumption_sum += consumption_state

                consumption_statistics.append(
                    StatisticData(
                        start=stat_time,
                        state=consumption_state,
                        sum=consumption_sum,
                    )
                )

        # Process production data (already fetched by _async_update_data)
        production_statistics = []
        if daily_production and daily_production.interval_reading:
            for reading in daily_production.interval_reading:
                reading_date = reading.date
                # Convert date to datetime at midnight UTC
                stat_time = datetime.combine(reading_date, datetime.min.time())
                stat_time = dt_util.as_utc(stat_time)

                # Skip if we already have this statistic
                if last_stats_time and stat_time.timestamp() <= last_stats_time:
                    continue

                # Value in Wh
                production_state = float(reading.value)
                production_sum += production_state

                production_statistics.append(
                    StatisticData(
                        start=stat_time,
                        state=production_state,
                        sum=production_sum,
                    )
                )

        # Add statistics to Home Assistant
        if consumption_statistics:
            _LOGGER.debug(
                "Adding %s consumption statistics",
                len(consumption_statistics),
            )
            async_add_external_statistics(self.hass, consumption_metadata, consumption_statistics)

        if production_statistics:
            _LOGGER.debug(
                "Adding %s production statistics",
                len(production_statistics),
            )
            async_add_external_statistics(self.hass, production_metadata, production_statistics)

    async def import_statistics(self, start: date, end: date) -> None:
        """Import statistics for a custom date range."""
        _LOGGER.info("Starting import of statistics from %s to %s", start, end)

        prm = self.client.prm

        # Define statistic IDs
        consumption_statistic_id = f"{DOMAIN}:{prm}_energy_consumption"
        production_statistic_id = f"{DOMAIN}:{prm}_energy_production"

        # Metadata for consumption statistics
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        )

        # Metadata for production statistics
        production_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Linky {prm} production",
            source=DOMAIN,
            statistic_id=production_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        )

        # Get last statistics to calculate proper sum
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )

        # Get the sum at the start date or initialize
        if last_stat:
            consumption_sum = float(last_stat[consumption_statistic_id][0].get("sum", 0))
        else:
            consumption_sum = 0.0

        last_prod_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, production_statistic_id, True, set()
        )
        production_sum = (
            float(last_prod_stat[production_statistic_id][0].get("sum", 0))
            if last_prod_stat
            else 0.0
        )

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

                    # Value in Wh
                    consumption_state = float(reading.value)
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

                    # Value in Wh
                    production_state = float(reading.value)
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
