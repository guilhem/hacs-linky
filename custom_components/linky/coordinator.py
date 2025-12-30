"""DataUpdateCoordinator for Linky integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pylinky import (
    APIError,
    AsyncLinkyClient,
    AuthenticationError,
    MeteringData,
)

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

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
        # Fetch data for the last 7 days to get recent consumption
        # The API returns data up to yesterday typically
        end = date.today()
        start = end - timedelta(days=7)

        data = LinkyData()

        try:
            # Fetch consumption data
            try:
                data.daily_consumption = await self.client.get_daily_consumption(
                    start=start, end=end
                )
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch daily consumption: %s", err)

            try:
                data.load_curve = await self.client.get_consumption_load_curve(
                    start=start, end=end
                )
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch load curve: %s", err)

            try:
                data.max_power = await self.client.get_max_power(start=start, end=end)
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch max power: %s", err)

            # Fetch production data (may fail if user has no solar panels)
            try:
                data.daily_production = await self.client.get_daily_production(
                    start=start, end=end
                )
            except AuthenticationError:
                raise
            except APIError as err:
                _LOGGER.debug("Failed to fetch daily production: %s", err)

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
        if (
            data.daily_consumption is None
            and data.load_curve is None
            and data.max_power is None
        ):
            raise UpdateFailed("Failed to fetch any consumption data from API")

        return data
