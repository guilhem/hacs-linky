"""Tests for Linky integration setup and unload."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pylinky import InvalidTokenError

from custom_components.linky.const import DOMAIN


async def test_setup_entry(
    hass: HomeAssistant,
    mock_linky_client: MagicMock,
    mock_config_entry,
) -> None:
    """Test successful setup of config entry."""
    result = await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert result is True
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_setup_entry_invalid_token(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test setup fails with invalid token."""
    with patch(
        "custom_components.linky.AsyncLinkyClient",
        side_effect=InvalidTokenError("Invalid token"),
    ):
        result = await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert result is False
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload_entry(
    hass: HomeAssistant,
    mock_linky_client: MagicMock,
    mock_config_entry,
) -> None:
    """Test unloading a config entry."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert result is True
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_creates_coordinator(
    hass: HomeAssistant,
    mock_linky_client: MagicMock,
    mock_config_entry,
) -> None:
    """Test that setup creates a coordinator in runtime_data."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.linky.coordinator import LinkyDataUpdateCoordinator

    assert mock_config_entry.runtime_data is not None
    assert isinstance(mock_config_entry.runtime_data, LinkyDataUpdateCoordinator)
