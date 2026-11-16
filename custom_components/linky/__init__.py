"""The Linky integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from threading import Lock
from typing import Any

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import AuthenticationError, LinkyClient, PRMAccessError
from .budget import ConsoBudgetError, async_get_conso_budget
from .conso_csv import read_conso_csv
from .const import (
    CONF_COLLECT_EXPORT,
    CONF_DAILY_ONLY,
    CONF_PRM,
    DEFAULT_COLLECT_EXPORT,
    DOMAIN,
    PARIS_TZ,
)
from .coordinator import LinkyDataUpdateCoordinator
from .statistics import Buckets, async_fill_hourly_consumption

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[Platform] = [Platform.SENSOR]

type LinkyConfigEntry = ConfigEntry[LinkyDataUpdateCoordinator]

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
SERVICE_REFRESH = "refresh"
SERVICE_GET_STATUS = "get_status"
SERVICE_IMPORT_CONSO_CSV = "import_conso_csv"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_FILE = "file"

METER_ACTION_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): str})
CSV_ACTION_SCHEMA = METER_ACTION_SCHEMA.extend(
    {vol.Required(ATTR_FILE): vol.Match(r"[0-9a-f]{32}\Z")}
)
SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): str,
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize scheduling state and register the Linky actions."""
    try:
        await async_get_conso_budget(hass).async_initialize()
    except ConsoBudgetError as err:
        _LOGGER.error("Could not initialize the Linky request budget: %s", err)

    def get_loaded_entry(call: ServiceCall) -> LinkyConfigEntry:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Select a loaded Linky meter")
        return entry

    async def run_entry_action[T](
        entry: LinkyConfigEntry, action: str, operation: Coroutine[Any, Any, T]
    ) -> T:
        task = entry.async_create_background_task(hass, operation, f"Linky {action}")
        try:
            return await task
        except ConfigEntryAuthFailed:
            entry.async_start_reauth(hass)
            raise
        except asyncio.CancelledError:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    f"The Linky meter was unloaded during {action}"
                ) from None
            raise

    async def handle_import_historical_data(call: ServiceCall) -> None:
        """Import statistics for the selected loaded meter."""
        entry = get_loaded_entry(call)

        today = dt_util.now(PARIS_TZ).date()
        start = call.data[ATTR_START_DATE]
        end = call.data.get(ATTR_END_DATE, today)

        if start >= end:
            raise ServiceValidationError("Start date must be before end date")
        if end > today:
            raise ServiceValidationError("End date cannot be after today in Paris")

        await run_entry_action(entry, "import", entry.runtime_data.import_statistics(start, end))

    async def handle_refresh(call: ServiceCall) -> ServiceResponse | None:
        """Run the same daily refresh as the scheduler."""
        entry = get_loaded_entry(call)
        result = await run_entry_action(entry, "refresh", entry.runtime_data.async_refresh_now())
        return result if call.return_response else None

    async def handle_get_status(call: ServiceCall) -> ServiceResponse:
        """Read local collection status without contacting Conso."""
        entry = get_loaded_entry(call)
        return await run_entry_action(entry, "status check", entry.runtime_data.async_get_status())

    # A native upload ID is consumed once, even when overlapping actions select it.
    upload_lock = Lock()

    def read_upload(file_id: str, prm: str) -> tuple[Buckets, list[str]]:
        try:
            with upload_lock, process_uploaded_file(hass, file_id) as path:
                return read_conso_csv(path, prm)
        except OSError, ValueError:
            raise ServiceValidationError(
                "Could not read the uploaded CSV. Select the file again."
            ) from None

    async def import_csv(file_id: str, prm: str) -> ServiceResponse:
        hourly, incomplete = await hass.async_add_executor_job(read_upload, file_id, prm)
        counts = await async_fill_hourly_consumption(hass, prm, hourly)
        return {**counts, "incomplete_hours": incomplete}

    async def handle_import_conso_csv(call: ServiceCall) -> ServiceResponse:
        """Fill missing hourly consumption statistics from a local CSV."""
        entry = get_loaded_entry(call)
        return await run_entry_action(
            entry, "CSV import", import_csv(call.data[ATTR_FILE], entry.data[CONF_PRM])
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_HISTORICAL_DATA,
        handle_import_historical_data,
        schema=SERVICE_IMPORT_HISTORICAL_DATA_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_REFRESH,
        handle_refresh,
        schema=METER_ACTION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_GET_STATUS,
        handle_get_status,
        schema=METER_ACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_CONSO_CSV,
        handle_import_conso_csv,
        schema=CSV_ACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Set up Linky from a config entry."""
    try:
        client = LinkyClient(
            async_get_clientsession(hass),
            entry.data[CONF_TOKEN],
            budget=async_get_conso_budget(hass),
            prm=entry.data[CONF_PRM],
        )
    except (AuthenticationError, PRMAccessError) as err:
        raise ConfigEntryAuthFailed(
            "Token is invalid or does not grant access to the meter"
        ) from err

    coordinator = LinkyDataUpdateCoordinator(
        hass,
        client,
        collect_export=entry.options.get(CONF_COLLECT_EXPORT, DEFAULT_COLLECT_EXPORT),
        daily_only=entry.options.get(CONF_DAILY_ONLY, False),
        config_entry=entry,
    )
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Unload the platforms; Home Assistant owns the shared HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
