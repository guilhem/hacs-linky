"""The Linky integration."""

from __future__ import annotations

import logging
from datetime import date, datetime

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from pylinky import (
    AsyncLinkyClient,
    AuthenticationError,
    InvalidTokenError,
    create_ssl_context,
)

from .const import CONF_PRM, DOMAIN
from .coordinator import LinkyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type LinkyConfigEntry = ConfigEntry[LinkyDataUpdateCoordinator]

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"

SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Set up Linky from a config entry."""
    token = entry.data[CONF_TOKEN]
    prm = entry.data[CONF_PRM]

    # Create SSL context in executor to avoid blocking the event loop
    ssl_context = await hass.async_add_executor_job(create_ssl_context)

    try:
        client = AsyncLinkyClient(
            token=token,
            prm=prm,
            user_agent="github.com/guilhem/hacs-linky",
            ssl_context=ssl_context,
        )
    except InvalidTokenError as err:
        raise ConfigEntryAuthFailed("Invalid token") from err

    coordinator = LinkyDataUpdateCoordinator(hass, client)

    try:
        await coordinator.async_config_entry_first_refresh()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Authentication failed") from err

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_import_historical_data(call: ServiceCall) -> None:
        """Handle the import_historical_data service call."""
        start_date = call.data[ATTR_START_DATE]
        end_date = call.data.get(ATTR_END_DATE, date.today())

        # Validate dates
        if start_date > end_date:
            raise ServiceValidationError("start_date must be before or equal to end_date")

        if end_date > date.today():
            raise ServiceValidationError("end_date cannot be in the future")

        _LOGGER.info(
            "Importing historical data from %s to %s for PRM %s",
            start_date,
            end_date,
            prm,
        )

        # Import the statistics
        await coordinator.import_statistics(start_date, end_date)

        _LOGGER.info("Historical data import completed")

    # Register the service only once per domain
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            handle_import_historical_data,
            schema=SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.close()

        # Unregister services if no more entries
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) == 1:  # This entry is the last one
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA)

    return unload_ok
