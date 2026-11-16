"""Test Linky polling and the bounded historical import."""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.linky.api import (
    APIError,
    AuthenticationError,
    HistoryLimitError,
    IntervalReading,
    LinkyClient,
    MeteringData,
    NoDataError,
)
from custom_components.linky.const import PARIS_TZ
from custom_components.linky.coordinator import LinkyData, LinkyDataUpdateCoordinator
from custom_components.linky.statistics import paris_midnight

_PRM = "12345678901234"
_START = date(2024, 1, 1)
_DAY = timedelta(days=1)


@pytest.fixture
def client() -> MagicMock:
    """Use the adapter interface and simulate unavailable optional readings."""
    client = MagicMock(spec=LinkyClient)
    client.prm = _PRM
    for name in (
        "get_daily_consumption",
        "get_daily_production",
        "get_consumption_load_curve",
        "get_production_load_curve",
        "get_max_power",
    ):
        getattr(client, name).side_effect = NoDataError("No measurements", 404)
    return client


@pytest.fixture
def write_statistics(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Isolate coordinator tests from Recorder and real provider pacing."""
    monkeypatch.setattr("custom_components.linky.coordinator.API_REQUEST_DELAY", 0)
    writer = AsyncMock()
    monkeypatch.setattr("custom_components.linky.coordinator.async_write_statistics", writer)
    return writer


async def _daily(start: date, end: date) -> MeteringData:
    return MeteringData(
        _PRM,
        start,
        end,
        [IntervalReading(start + i * _DAY, 1500) for i in range((end - start).days)],
        "Wh",
    )


@pytest.mark.parametrize("direction", ["consumption", "production"])
async def test_refresh_overlaps_and_accepts_one_direction(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock, direction: str
) -> None:
    """Poll a rolling Paris week and publish daily data without inferring hourly data."""
    daily = getattr(client, f"get_daily_{direction}")
    daily.side_effect = _daily
    client.get_max_power.side_effect = APIError("Provider unavailable", 502)
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    today = dt_util.now(PARIS_TZ).date()
    start = today - 7 * _DAY

    for _ in range(2):
        data = await coordinator._async_update_data()
        assert getattr(data, f"daily_{direction}") == await _daily(start, today)
        assert data.max_power is None
        assert data.load_curve is None
        assert data.production_load_curve is None
    assert daily.await_args_list == [call(start, today)] * 2
    assert (
        write_statistics.await_args_list
        == [
            call(
                hass,
                _PRM,
                {(direction, "daily"): {paris_midnight(start + i * _DAY): 1.5 for i in range(7)}},
            )
        ]
        * 2
    )


async def test_only_polling_shortens_a_window_that_predates_activation(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish the available suffix, while a manual import reports the rejected range."""
    monkeypatch.setattr("custom_components.linky.api.API_REQUEST_DELAY", 0)
    today = dt_util.now(PARIS_TZ).date()
    activated = today - 3 * _DAY
    history_error = HistoryLimitError("Requested data predates available meter history", 400)

    async def recent_daily(start: date, end: date) -> MeteringData:
        if start < activated:
            raise history_error
        return await _daily(start, end)

    client.get_daily_consumption.side_effect = recent_daily
    client.get_daily_production.side_effect = history_error
    coordinator = LinkyDataUpdateCoordinator(hass, client)

    data = await coordinator._async_update_data()
    assert data.daily_consumption == await _daily(activated, today)
    assert data.daily_production is None
    assert client.get_daily_consumption.await_args_list == [
        call(today - days * _DAY, today) for days in (7, 3, 5, 4)
    ]
    assert client.get_daily_production.await_args_list == [
        call(today - days * _DAY, today) for days in (7, 3, 1)
    ]
    write_statistics.assert_awaited_once_with(
        hass,
        _PRM,
        {("consumption", "daily"): {paris_midnight(activated + i * _DAY): 1.5 for i in range(3)}},
    )

    client.get_daily_consumption.reset_mock()
    write_statistics.reset_mock()
    with pytest.raises(HomeAssistantError, match="predates available meter history"):
        await coordinator.import_statistics(today - 7 * _DAY, today)
    client.get_daily_consumption.assert_awaited_once_with(start=today - 7 * _DAY, end=today)
    write_statistics.assert_not_awaited()


async def test_refresh_maps_authentication_and_connectivity(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Report invalid credentials separately from network failure or absent data."""
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    assert await coordinator._async_update_data() == LinkyData()
    write_statistics.assert_not_awaited()
    client.get_daily_consumption.side_effect = APIError("Could not connect to Conso API")
    with pytest.raises(UpdateFailed, match="Could not connect"):
        await coordinator._async_update_data()
    client.get_daily_consumption.side_effect = AuthenticationError("Token rejected", 401)
    with pytest.raises(ConfigEntryAuthFailed, match="authentication failed"):
        await coordinator._async_update_data()
    write_statistics.assert_not_awaited()


async def test_first_refresh_does_not_block_startup(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Keep sensor data available during startup and publish it once Recorder can run."""
    wrote = asyncio.Event()
    write_statistics.side_effect = lambda *_args: wrote.set()
    hass.set_state(CoreState.starting)
    client.get_daily_consumption.side_effect = _daily
    coordinator = LinkyDataUpdateCoordinator(hass, client)

    assert (await coordinator._async_update_data()).daily_consumption is not None
    write_statistics.assert_not_awaited()
    with pytest.raises(HomeAssistantError, match="finish starting"):
        await coordinator.import_statistics(_START, _START + _DAY)

    hass.set_state(CoreState.running)
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STARTED)
    async with asyncio.timeout(3):
        await wrote.wait()
    write_statistics.assert_awaited_once()
    client.get_daily_consumption.assert_awaited_once()


async def test_import_splits_curves_but_allows_daily_only(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Treat absent consent as expected and validate request windows."""
    client.get_daily_consumption.side_effect = _daily
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    end = _START + 8 * _DAY

    with pytest.raises(ServiceValidationError, match="at most 30 days"):
        await coordinator.import_statistics(_START, _START + 31 * _DAY)
    client.get_daily_consumption.assert_not_awaited()

    await coordinator.import_statistics(_START, end)
    client.get_daily_consumption.assert_awaited_once_with(start=_START, end=end)
    assert client.get_consumption_load_curve.await_args_list == [
        call(start=_START, end=_START + 7 * _DAY),
        call(start=_START + 7 * _DAY, end=end),
    ]
    client.get_max_power.assert_not_awaited()
    assert write_statistics.await_args.args[2][("consumption", "hourly")] == {}
    assert len(write_statistics.await_args.args[2][("consumption", "daily")]) == 8


async def test_import_reports_no_data_and_partial_rate_limit(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Preserve already fetched data and report that other series could not be imported."""
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    end = _START + _DAY
    with pytest.raises(ServiceValidationError, match="No Linky energy measurements"):
        await coordinator.import_statistics(_START, end)
    write_statistics.assert_not_awaited()

    client.get_daily_consumption.side_effect = _daily
    client.get_consumption_load_curve.side_effect = APIError("Rate limit reached", 429)
    client.get_production_load_curve.reset_mock()
    with pytest.raises(HomeAssistantError, match="partially imported: load_curve.*Rate limit"):
        await coordinator.import_statistics(_START, end)
    assert write_statistics.await_args.args[2][("consumption", "daily")] == {
        paris_midnight(_START): 1.5
    }
    client.get_production_load_curve.assert_not_awaited()


async def test_import_reports_incomplete_series(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Import only complete hours and surface missing buckets in an active series."""
    end = _START + _DAY
    half = timedelta(minutes=30)
    midnight = paris_midnight(_START)
    client.get_consumption_load_curve.side_effect = None
    client.get_consumption_load_curve.return_value = MeteringData(
        _PRM,
        _START,
        end,
        [IntervalReading(midnight + slot * half, 0, half) for slot in (1, 2, 3)],
        "W",
    )
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    with pytest.raises(HomeAssistantError, match="23 missing or invalid hours"):
        await coordinator.import_statistics(_START, end)
    assert write_statistics.await_args.args[2][("consumption", "hourly")] == {midnight: 0}


async def test_refresh_waits_for_import_write(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Provider reads from polling cannot race a historical import still writing."""
    client.get_daily_consumption.side_effect = _daily
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    writing = asyncio.Event()
    committed = asyncio.Event()

    async def write(*_args) -> None:
        writing.set()
        await committed.wait()

    write_statistics.side_effect = write
    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        group.create_task(coordinator.import_statistics(_START, _START + _DAY))
        await writing.wait()
        group.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)
        client.get_daily_consumption.assert_awaited_once()
        committed.set()
    assert client.get_daily_consumption.await_count == 2
