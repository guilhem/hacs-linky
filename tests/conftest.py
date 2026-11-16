"""Fixtures for Linky integration tests."""

from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from shutil import copytree
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from aiohttp import ClientSession
from homeassistant import auth, loader
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import User
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components.recorder.core import Recorder
from homeassistant.config_entries import SOURCE_USER, ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import async_initialize_recorder, get_instance
from homeassistant.setup import async_setup_component

from custom_components.linky.api import IntervalReading, MeteringData
from custom_components.linky.const import CONF_PRM, DOMAIN

PRM = "12345678901234"
OTHER_PRM = "98765432109876"
JWT_TEST_KEY = "a disposable key for Linky tests"
START = date(2024, 1, 1)
END = date(2024, 1, 8)


@pytest.fixture
def mock_http_session() -> MagicMock:
    """Supply an HTTP session that never connects to Conso."""
    return MagicMock(spec=ClientSession)


@pytest.fixture
async def hass(tmp_path: Path, mock_http_session: MagicMock) -> AsyncIterator[HomeAssistant]:
    """Start real Home Assistant with the custom integration and SQLite."""
    copytree(Path(__file__).parents[1] / "custom_components", tmp_path / "custom_components")
    home = HomeAssistant(str(tmp_path))
    home.config.skip_pip = True
    loader.async_setup(home)
    home.config_entries = ConfigEntries(home, {})
    await loader.async_get_custom_components(home)
    assert await async_load_base_functionality(home)
    home.auth = await auth.auth_manager_from_config(home, [], [])
    async_initialize_recorder(home)
    assert await async_setup_component(home, "recorder", {"recorder": {"auto_purge": False}})
    await home.async_start()
    try:
        with (
            patch(
                "custom_components.linky.async_get_clientsession", return_value=mock_http_session
            ),
            patch(
                "custom_components.linky.config_flow.async_get_clientsession",
                return_value=mock_http_session,
            ),
        ):
            yield home
    finally:
        for entry in home.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await home.config_entries.async_unload(entry.entry_id)
        await home.async_stop(force=True)


@pytest.fixture
def recorder_mock(hass: HomeAssistant) -> Recorder:
    """Return the real Recorder and its temporary database."""
    return get_instance(hass)


@pytest.fixture
async def hass_read_only_user(hass: HomeAssistant) -> User:
    """Create a real user without access to admin actions."""
    await hass.auth.async_create_user("Owner")
    return await hass.auth.async_create_user("Read only", group_ids=[GROUP_ID_READ_ONLY])


@pytest.fixture
def add_linky_entry(
    hass: HomeAssistant,
) -> Callable[[str, str], Coroutine[Any, Any, ConfigEntry]]:
    """Register a Linky entry in the same state as one read from storage."""

    async def add(prm: str, token: str) -> ConfigEntry:
        entry = ConfigEntry(
            domain=DOMAIN,
            title=f"Linky {prm}",
            data={"token": token, CONF_PRM: prm},
            unique_id=prm,
            options={},
            source=SOURCE_USER,
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        with patch.object(hass.config_entries, "async_setup", return_value=False):
            await hass.config_entries.async_add(entry)
        return entry

    return add


@pytest.fixture
def single_prm_token() -> str:
    """JWT token with a single PRM."""
    return jwt.encode({"sub": [PRM]}, JWT_TEST_KEY, algorithm="HS256")


@pytest.fixture
def multi_prm_token() -> str:
    """JWT token with multiple PRMs."""
    return jwt.encode({"sub": [PRM, OTHER_PRM]}, JWT_TEST_KEY, algorithm="HS256")


@pytest.fixture
def daily_consumption_data() -> MeteringData:
    """Sample daily consumption data."""
    return MeteringData(
        PRM,
        START,
        END,
        [
            IntervalReading(START + timedelta(days=offset), wh)
            for offset, wh in enumerate([11776, 14401, 12820, 13500, 11200, 15600, 12100])
        ],
        "Wh",
        "BRUT",
    )


@pytest.fixture
def load_curve_data() -> MeteringData:
    """Sample load curve data, including end times and duration."""
    duration = timedelta(minutes=30)
    return MeteringData(
        PRM,
        START,
        END,
        [
            IntervalReading(datetime(2024, 1, 7) + duration * offset, watts, duration)
            for offset, watts in enumerate([450, 380, 520])
        ],
        "W",
    )


@pytest.fixture
def max_power_data() -> MeteringData:
    """Sample daily peak apparent power."""
    return MeteringData(
        PRM,
        START,
        END,
        [
            IntervalReading(datetime(2024, 1, 1, 8), 6200),
            IntervalReading(datetime(2024, 1, 2, 10), 7100),
            IntervalReading(datetime(2024, 1, 7, 19), 5800),
        ],
        "VA",
    )


def _create_mock_client(
    daily_consumption: MeteringData,
    load_curve: MeteringData,
    peak: MeteringData,
    prms: list[str],
) -> Iterator[MagicMock]:
    """Replace only the provider at the integration boundary."""
    client = MagicMock(
        prm=prms[0],
        prms=prms,
        get_daily_consumption=AsyncMock(return_value=daily_consumption),
        get_consumption_load_curve=AsyncMock(return_value=load_curve),
        get_max_power=AsyncMock(return_value=peak),
        get_daily_production=AsyncMock(return_value=None),
        get_production_load_curve=AsyncMock(return_value=None),
    )
    with (
        patch("custom_components.linky.LinkyClient", return_value=client),
        patch("custom_components.linky.config_flow.LinkyClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_linky_client(
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> Iterator[MagicMock]:
    """Simulate Conso for a single meter."""
    yield from _create_mock_client(daily_consumption_data, load_curve_data, max_power_data, [PRM])


@pytest.fixture
def mock_linky_client_multi_prm(
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    max_power_data: MeteringData,
) -> Iterator[MagicMock]:
    """Simulate Conso for multiple meters."""
    yield from _create_mock_client(
        daily_consumption_data, load_curve_data, max_power_data, [PRM, OTHER_PRM]
    )


@pytest.fixture
async def mock_config_entry(add_linky_entry, single_prm_token: str) -> ConfigEntry:
    """Add a registered (unloaded) config entry."""
    return await add_linky_entry(PRM, single_prm_token)
