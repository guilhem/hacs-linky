"""The Linky integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinkyConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.close()

    return unload_ok
