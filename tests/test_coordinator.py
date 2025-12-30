"""Tests for the Linky DataUpdateCoordinator."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pylinky import APIError, AuthenticationError, MeteringData

from custom_components.linky.coordinator import LinkyData, LinkyDataUpdateCoordinator


async def test_coordinator_update_success(
    hass: HomeAssistant,
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> None:
    """Test successful data update."""
    mock_client = AsyncMock()
    mock_client.get_daily_consumption = AsyncMock(return_value=daily_consumption_data)
    mock_client.get_consumption_load_curve = AsyncMock(return_value=load_curve_data)
    mock_client.get_max_power = AsyncMock(return_value=max_power_data)
    mock_client.get_daily_production = AsyncMock(return_value=None)
    mock_client.get_production_load_curve = AsyncMock(return_value=None)

    coordinator = LinkyDataUpdateCoordinator(hass, mock_client)
    data = await coordinator._async_update_data()

    assert isinstance(data, LinkyData)
    assert data.daily_consumption == daily_consumption_data
    assert data.load_curve == load_curve_data
    assert data.max_power == max_power_data
    assert data.daily_production is None
    assert data.production_load_curve is None


async def test_coordinator_update_with_production(
    hass: HomeAssistant,
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> None:
    """Test data update with production data."""
    production_data = MeteringData.from_dict({
        "usage_point_id": "12345678901234",
        "start": "2024-01-01",
        "end": "2024-01-08",
        "quality": "BRUT",
        "reading_type": {
            "unit": "Wh",
            "measurement_kind": "energy",
            "aggregate": "sum",
            "measuring_period": "P1D",
        },
        "interval_reading": [
            {"value": "5000", "date": "2024-01-07"},
        ],
    })

    mock_client = AsyncMock()
    mock_client.get_daily_consumption = AsyncMock(return_value=daily_consumption_data)
    mock_client.get_consumption_load_curve = AsyncMock(return_value=load_curve_data)
    mock_client.get_max_power = AsyncMock(return_value=max_power_data)
    mock_client.get_daily_production = AsyncMock(return_value=production_data)
    mock_client.get_production_load_curve = AsyncMock(return_value=None)

    coordinator = LinkyDataUpdateCoordinator(hass, mock_client)
    data = await coordinator._async_update_data()

    assert data.daily_production == production_data


async def test_coordinator_partial_failure(
    hass: HomeAssistant,
    daily_consumption_data: MeteringData,
) -> None:
    """Test that partial API failures don't fail the entire update."""
    mock_client = AsyncMock()
    mock_client.get_daily_consumption = AsyncMock(return_value=daily_consumption_data)
    mock_client.get_consumption_load_curve = AsyncMock(
        side_effect=APIError(400, "No data available")
    )
    mock_client.get_max_power = AsyncMock(side_effect=APIError(400, "No data available"))
    mock_client.get_daily_production = AsyncMock(side_effect=APIError(400, "No data"))
    mock_client.get_production_load_curve = AsyncMock(side_effect=APIError(400, "No data"))

    coordinator = LinkyDataUpdateCoordinator(hass, mock_client)
    data = await coordinator._async_update_data()

    # Should succeed with partial data
    assert data.daily_consumption == daily_consumption_data
    assert data.load_curve is None
    assert data.max_power is None


async def test_coordinator_authentication_error(hass: HomeAssistant) -> None:
    """Test that authentication errors raise UpdateFailed."""
    mock_client = AsyncMock()
    # AuthenticationError is a subclass of APIError, so we need to raise it
    # before any other API call
    mock_client.get_daily_consumption = AsyncMock(
        side_effect=AuthenticationError("Token expired")
    )
    mock_client.get_consumption_load_curve = AsyncMock(
        side_effect=AuthenticationError("Token expired")
    )
    mock_client.get_max_power = AsyncMock(
        side_effect=AuthenticationError("Token expired")
    )
    mock_client.get_daily_production = AsyncMock(
        side_effect=AuthenticationError("Token expired")
    )
    mock_client.get_production_load_curve = AsyncMock(
        side_effect=AuthenticationError("Token expired")
    )

    coordinator = LinkyDataUpdateCoordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Authentication failed"):
        await coordinator._async_update_data()


async def test_coordinator_no_data(hass: HomeAssistant) -> None:
    """Test that complete failure to fetch data raises UpdateFailed."""
    mock_client = AsyncMock()
    mock_client.get_daily_consumption = AsyncMock(side_effect=APIError(400, "No data"))
    mock_client.get_consumption_load_curve = AsyncMock(side_effect=APIError(400, "No data"))
    mock_client.get_max_power = AsyncMock(side_effect=APIError(400, "No data"))
    mock_client.get_daily_production = AsyncMock(side_effect=APIError(400, "No data"))
    mock_client.get_production_load_curve = AsyncMock(side_effect=APIError(400, "No data"))

    coordinator = LinkyDataUpdateCoordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Failed to fetch any consumption data"):
        await coordinator._async_update_data()


async def test_linky_data_dataclass() -> None:
    """Test LinkyData dataclass defaults."""
    data = LinkyData()

    assert data.daily_consumption is None
    assert data.load_curve is None
    assert data.max_power is None
    assert data.daily_production is None
    assert data.production_load_curve is None
