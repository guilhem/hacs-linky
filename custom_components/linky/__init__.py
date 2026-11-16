"""The Linky integration."""

from __future__ import annotations

import asyncio

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import AuthenticationError, LinkyClient, PRMAccessError
from .const import (
    CONF_COLLECT_EXPORT,
    CONF_PRM,
    CONF_SCAN_INTERVAL,
    DEFAULT_COLLECT_EXPORT,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    PARIS_TZ,
)
from .coordinator import LinkyDataUpdateCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[Platform] = [Platform.SENSOR]

type LinkyConfigEntry = ConfigEntry[LinkyDataUpdateCoordinator]

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"

SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): str,
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Linky action once for all meters."""

    async def handle_import_historical_data(call: ServiceCall) -> None:
        """Import statistics for the selected loaded meter."""
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Select a loaded Linky meter")

        today = dt_util.now(PARIS_TZ).date()
        start = call.data[ATTR_START_DATE]
        end = call.data.get(ATTR_END_DATE, today)

        if start >= end:
            raise ServiceValidationError("Start date must be before end date")
        if end > today:
            raise ServiceValidationError("End date cannot be after today in Paris")

        task = entry.async_create_background_task(
            hass, entry.runtime_data.import_statistics(start, end), "Import Linky statistics"
        )
        try:
            await task
        except ConfigEntryAuthFailed:
            entry.async_start_reauth(hass)
            raise
        except asyncio.CancelledError:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError("The Linky meter was unloaded during import") from None
            raise

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_HISTORICAL_DATA,
        handle_import_historical_data,
        schema=SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Set up Linky from a config entry."""
    try:
        client = LinkyClient(
            async_get_clientsession(hass), entry.data[CONF_TOKEN], prm=entry.data[CONF_PRM]
        )
    except (AuthenticationError, PRMAccessError) as err:
        raise ConfigEntryAuthFailed(
            "Token is invalid or does not grant access to the meter"
        ) from err

    coordinator = LinkyDataUpdateCoordinator(
        hass,
        client,
        scan_interval_hours=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_HOURS),
        collect_export=entry.options.get(CONF_COLLECT_EXPORT, DEFAULT_COLLECT_EXPORT),
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Unload the platforms; Home Assistant owns the shared HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
