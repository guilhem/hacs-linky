"""Tests for Linky sensors."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    STATE_UNAVAILABLE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pylinky import MeteringData

from custom_components.linky.const import DOMAIN


async def test_sensors_created(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
) -> None:
    """Test that sensors are created."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)

    # Check consumption sensors are created
    assert entity_registry.async_get("sensor.linky_12345678901234_daily_consumption")
    assert entity_registry.async_get("sensor.linky_12345678901234_total_consumption_7_days")
    assert entity_registry.async_get("sensor.linky_12345678901234_current_power")
    assert entity_registry.async_get("sensor.linky_12345678901234_maximum_power")

    # Production sensors should be created but disabled by default
    entry = entity_registry.async_get("sensor.linky_12345678901234_daily_production")
    assert entry is not None
    assert entry.disabled_by is not None


async def test_daily_consumption_sensor(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
    daily_consumption_data: MeteringData,
) -> None:
    """Test daily consumption sensor values."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.linky_12345678901234_daily_consumption")
    assert state is not None
    # Last reading value
    assert state.state == "12100"
    assert state.attributes["unit_of_measurement"] == UnitOfEnergy.WATT_HOUR
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["usage_point_id"] == "12345678901234"
    assert state.attributes["quality"] == "BRUT"


async def test_total_consumption_week_sensor(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
    daily_consumption_data: MeteringData,
) -> None:
    """Test total consumption week sensor."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.linky_12345678901234_total_consumption_7_days")
    assert state is not None
    # Sum of all readings
    expected_total = sum(r.value for r in daily_consumption_data.interval_reading)
    assert state.state == str(expected_total)


async def test_current_power_sensor(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
) -> None:
    """Test current power sensor."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.linky_12345678901234_current_power")
    assert state is not None
    # Last load curve value
    assert state.state == "520"
    assert state.attributes["unit_of_measurement"] == UnitOfPower.WATT
    assert state.attributes["device_class"] == "power"


async def test_max_power_sensor(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
) -> None:
    """Test max power sensor."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.linky_12345678901234_maximum_power")
    assert state is not None
    # Last max power value
    assert state.state == "5800"
    assert state.attributes["unit_of_measurement"] == "VA"
    assert state.attributes["device_class"] == "apparent_power"


async def test_sensor_unavailable_when_no_data(
    hass: HomeAssistant,
    single_prm_token: str,
    mock_config_entry,
) -> None:
    """Test sensors are unavailable when data is missing."""
    with patch("custom_components.linky.AsyncLinkyClient") as mock_client_class:
        mock_instance = AsyncMock()
        mock_instance.prm = "12345678901234"
        mock_instance.prms = ["12345678901234"]
        mock_instance.get_daily_consumption = AsyncMock(return_value=None)
        mock_instance.get_consumption_load_curve = AsyncMock(return_value=None)
        mock_instance.get_max_power = AsyncMock(return_value=None)
        mock_instance.get_daily_production = AsyncMock(return_value=None)
        mock_instance.get_production_load_curve = AsyncMock(return_value=None)
        mock_instance.close = AsyncMock()
        mock_client_class.return_value = mock_instance

        # This will fail because no consumption data at all
        result = await hass.config_entries.async_setup(mock_config_entry.entry_id)
        # Setup should fail because coordinator fails
        assert result is False


async def test_device_info(
    hass: HomeAssistant,
    mock_linky_client: AsyncMock,
    mock_config_entry,
) -> None:
    """Test device info is properly set."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get("sensor.linky_12345678901234_daily_consumption")

    assert entry is not None
    assert entry.device_id is not None

    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(entry.device_id)

    assert device is not None
    assert device.manufacturer == "Enedis"
    assert device.model == "Linky"
    assert (DOMAIN, "12345678901234") in device.identifiers
