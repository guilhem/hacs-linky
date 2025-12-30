"""Fixtures for Linky integration tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from homeassistant.core import HomeAssistant
from pylinky import MeteringData

from custom_components.linky.const import CONF_PRM, DOMAIN

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


@pytest.fixture
def single_prm_token() -> str:
    """JWT token with a single PRM."""
    return jwt.encode({"sub": "12345678901234"}, "secret", algorithm="HS256")


@pytest.fixture
def multi_prm_token() -> str:
    """JWT token with multiple PRMs."""
    return jwt.encode(
        {"sub": ["12345678901234", "98765432109876"]},
        "secret",
        algorithm="HS256",
    )


@pytest.fixture
def daily_consumption_data() -> MeteringData:
    """Sample daily consumption data."""
    return MeteringData.from_dict({
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
            {"value": "11776", "date": "2024-01-01"},
            {"value": "14401", "date": "2024-01-02"},
            {"value": "12820", "date": "2024-01-03"},
            {"value": "13500", "date": "2024-01-04"},
            {"value": "11200", "date": "2024-01-05"},
            {"value": "15600", "date": "2024-01-06"},
            {"value": "12100", "date": "2024-01-07"},
        ],
    })


@pytest.fixture
def load_curve_data() -> MeteringData:
    """Sample load curve data."""
    return MeteringData.from_dict({
        "usage_point_id": "12345678901234",
        "start": "2024-01-07",
        "end": "2024-01-08",
        "quality": "BRUT",
        "reading_type": {
            "unit": "W",
            "measurement_kind": "power",
            "aggregate": "average",
        },
        "interval_reading": [
            {"value": "450", "date": "2024-01-07T00:00:00", "interval_length": "PT30M"},
            {"value": "380", "date": "2024-01-07T00:30:00", "interval_length": "PT30M"},
            {"value": "520", "date": "2024-01-07T01:00:00", "interval_length": "PT30M"},
        ],
    })


@pytest.fixture
def max_power_data() -> MeteringData:
    """Sample max power data."""
    return MeteringData.from_dict({
        "usage_point_id": "12345678901234",
        "start": "2024-01-01",
        "end": "2024-01-08",
        "quality": "BRUT",
        "reading_type": {
            "unit": "VA",
            "measurement_kind": "power",
            "aggregate": "maximum",
            "measuring_period": "P1D",
        },
        "interval_reading": [
            {"value": "6200", "date": "2024-01-01", "measure_type": "B"},
            {"value": "7100", "date": "2024-01-02", "measure_type": "B"},
            {"value": "5800", "date": "2024-01-07", "measure_type": "B"},
        ],
    })


def _create_mock_client(
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
    prms: list[str],
) -> MagicMock:
    """Create a mock AsyncLinkyClient."""
    mock_instance = MagicMock()
    mock_instance.prm = prms[0]
    mock_instance.prms = prms
    mock_instance.get_daily_consumption = AsyncMock(return_value=daily_consumption_data)
    mock_instance.get_consumption_load_curve = AsyncMock(return_value=load_curve_data)
    mock_instance.get_max_power = AsyncMock(return_value=max_power_data)
    mock_instance.get_daily_production = AsyncMock(return_value=None)
    mock_instance.get_production_load_curve = AsyncMock(return_value=None)
    mock_instance.close = AsyncMock()
    return mock_instance


@pytest.fixture
def mock_linky_client(
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> Generator[MagicMock, None, None]:
    """Mock AsyncLinkyClient."""
    mock_instance = _create_mock_client(
        daily_consumption_data, load_curve_data, max_power_data,
        ["12345678901234"]
    )

    with patch(
        "custom_components.linky.AsyncLinkyClient", return_value=mock_instance
    ), patch(
        "custom_components.linky.config_flow.AsyncLinkyClient", return_value=mock_instance
    ):
        yield mock_instance


@pytest.fixture
def mock_linky_client_multi_prm(
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> Generator[MagicMock, None, None]:
    """Mock AsyncLinkyClient with multiple PRMs."""
    mock_instance = _create_mock_client(
        daily_consumption_data, load_curve_data, max_power_data,
        ["12345678901234", "98765432109876"]
    )

    with patch(
        "custom_components.linky.AsyncLinkyClient", return_value=mock_instance
    ), patch(
        "custom_components.linky.config_flow.AsyncLinkyClient", return_value=mock_instance
    ):
        yield mock_instance


@pytest.fixture
async def mock_config_entry(hass: HomeAssistant, single_prm_token: str):
    """Create a mock config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Linky 12345678901234",
        data={
            "token": single_prm_token,
            CONF_PRM: "12345678901234",
        },
        unique_id="12345678901234",
    )
    entry.add_to_hass(hass)
    return entry
