"""Test Linky daily scheduling, measurements, and bounded imports."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
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
from custom_components.linky.budget import (
    ConsoBudget,
    ConsoBudgetError,
    ConsoBudgetExceeded,
    async_get_conso_budget,
)
from custom_components.linky.const import DOMAIN, PARIS_TZ
from custom_components.linky.coordinator import LinkyData, LinkyDataUpdateCoordinator
from custom_components.linky.statistics import paris_midnight

_PRM = "12345678901234"
_START = date(2024, 3, 31)
_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)
_ENDPOINTS = (
    "get_daily_consumption",
    "get_daily_production",
    "get_consumption_load_curve",
    "get_production_load_curve",
)


@pytest.fixture
def client() -> MagicMock:
    """Use the adapter interface and simulate unavailable readings by default."""
    client = MagicMock(spec=LinkyClient)
    client.prm = _PRM
    for name in _ENDPOINTS:
        getattr(client, name).side_effect = NoDataError("No measurements", 404)
    return client


@pytest.fixture
def write_statistics(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Observe what the coordinator sends to Recorder."""
    writer = AsyncMock()
    monkeypatch.setattr("custom_components.linky.coordinator.async_write_statistics", writer)
    return writer


@pytest.fixture
def allow_every_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow multiple provider outcomes when testing the repair and auth paths."""
    budget = MagicMock()
    budget.daily_batch.return_value.__aenter__.return_value = True
    monkeypatch.setattr(
        "custom_components.linky.coordinator.async_get_conso_budget", lambda _hass: budget
    )


async def _daily(start: date, end: date) -> MeteringData:
    return MeteringData(
        _PRM,
        start,
        end,
        [IntervalReading(start + i * _DAY, 1001) for i in range((end - start).days)],
        "Wh",
    )


async def _curve(start: date, end: date) -> MeteringData:
    first = paris_midnight(start)
    hours = (paris_midnight(end) - first) // _HOUR
    return MeteringData(
        _PRM,
        start,
        end,
        [IntervalReading(first + i * _HOUR, 500, _HOUR) for i in range(1, hours + 1)],
        "W",
    )


async def test_daily_slot_claim_precedes_refresh_and_survives_reload(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    mock_config_entry: ConfigEntry,
) -> None:
    """Register without network traffic and leave sensors alone on a duplicate slot."""
    coordinator = LinkyDataUpdateCoordinator(hass, client, config_entry=mock_config_entry)
    assert coordinator.update_interval is None
    assert coordinator.data is None
    cancel = MagicMock(return_value=None)
    notify = MagicMock()
    coordinator.async_add_listener(notify)

    with patch(
        "custom_components.linky.coordinator.async_track_utc_time_change", return_value=cancel
    ) as track:
        coordinator.async_start()
        track.assert_called_once()
        slot = track.call_args.args[1]
        minute = track.call_args.kwargs["minute"]
        assert track.call_args.kwargs == {"hour": range(7, 22), "minute": minute, "second": 0}
        assert 30 <= minute <= 59

    assert not client.mock_calls
    write_statistics.assert_not_awaited()
    assert coordinator.data is None

    today = dt_util.now(PARIS_TZ).date()
    when = datetime(today.year, today.month, today.day, 7, minute, tzinfo=UTC)
    budget_after_restart = ConsoBudget(hass)

    async def check_claim(start: date, end: date) -> MeteringData:
        async with budget_after_restart.daily_batch(_PRM, today) as claimed:
            assert not claimed
        return await _curve(start, end)

    client.get_consumption_load_curve.side_effect = check_claim
    slot(when)
    await asyncio.gather(*mock_config_entry._background_tasks)
    client.get_consumption_load_curve.assert_awaited_once_with(start=today - 7 * _DAY, end=today)
    write_statistics.assert_awaited_once()
    assert coordinator.data.load_curve is not None
    notify.assert_called_once()

    slot(when)
    await asyncio.gather(*mock_config_entry._background_tasks)
    notify.assert_called_once()
    await mock_config_entry._async_process_on_unload(hass)
    cancel.assert_called_once()

    reloaded = LinkyDataUpdateCoordinator(hass, client, config_entry=mock_config_entry)
    notify.reset_mock()
    reloaded.async_add_listener(notify)
    with (
        patch(
            "custom_components.linky.coordinator.async_track_utc_time_change", return_value=cancel
        ) as track,
        patch(
            "custom_components.linky.coordinator.async_get_conso_budget",
            return_value=budget_after_restart,
        ),
    ):
        reloaded.async_start()
        assert track.call_args.kwargs["minute"] == minute
        track.call_args.args[1](when)
        await asyncio.gather(*mock_config_entry._background_tasks)

    assert reloaded.data is None
    notify.assert_not_called()
    client.get_consumption_load_curve.assert_awaited_once()
    write_statistics.assert_awaited_once()

    await mock_config_entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    "first_error",
    [None, APIError("Unavailable", 503), ConsoBudgetExceeded("No capacity")],
    ids=["successful", "provider_failed", "exhausted"],
)
async def test_generic_refreshes_share_one_daily_attempt(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    first_error: Exception | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A same-day generic refresh preserves the earlier result, even if it failed."""
    now = dt_util.now(PARIS_TZ)
    monkeypatch.setattr(
        "custom_components.linky.coordinator.dt_util", SimpleNamespace(now=lambda _tz: now)
    )
    coordinator = LinkyDataUpdateCoordinator(hass, client, collect_export=False)
    notify = MagicMock()
    coordinator.async_add_listener(notify)
    consumption = client.get_consumption_load_curve
    consumption.side_effect = first_error if first_error else _curve

    await coordinator.async_refresh()
    first_data = coordinator.data
    last_exception = coordinator.last_exception
    update_succeeded = not isinstance(first_error, APIError)
    notification_count = 0 if isinstance(first_error, ConsoBudgetExceeded) else 1
    assert coordinator.last_update_success == update_succeeded
    assert (first_data is None) == (first_error is not None)
    assert notify.call_count == notification_count

    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
    assert "data recovered" not in caplog.text
    consumption.assert_awaited_once()
    assert coordinator.data is first_data
    assert coordinator.last_exception is last_exception
    assert coordinator.last_update_success == update_succeeded
    assert notify.call_count == notification_count
    assert write_statistics.await_count == (0 if first_error else 1)

    now += _DAY
    consumption.side_effect = _curve
    await coordinator.async_refresh()
    assert consumption.await_count == 2
    assert coordinator.last_update_success
    assert coordinator.data.daily_consumption.end == now.date()
    assert notify.call_count == notification_count + 1


async def test_export_defers_until_the_batch_fits_in_the_rolling_budget(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    mock_config_entry: ConfigEntry,
    budget_clock,
) -> None:
    """Schedule the next eligible check in seconds, without spending the last request."""
    budget = async_get_conso_budget(hass)
    for _ in range(2):
        async with budget.request(_PRM):
            pass
    budget_clock.elapsed += 24 * 60 * 60 - 12
    coordinator = LinkyDataUpdateCoordinator(
        hass, client, config_entry=mock_config_entry, collect_export=True
    )
    client.get_consumption_load_curve.side_effect = _curve
    client.get_production_load_curve.side_effect = _curve

    with (
        freeze_time("2026-11-16 08:00:00", real_asyncio=True),
        patch("custom_components.linky.coordinator.async_call_later") as call_later,
    ):
        await coordinator.async_refresh()
        client.get_consumption_load_curve.assert_not_awaited()
        client.get_production_load_curve.assert_not_awaited()
        call_later.assert_called_once()
        _, delay, callback = call_later.call_args.args
        assert delay == 12

        budget_clock.elapsed += 12
        callback(dt_util.utcnow())
        await asyncio.gather(*mock_config_entry._background_tasks)
        client.get_consumption_load_curve.assert_awaited_once()
        client.get_production_load_curve.assert_awaited_once()
        write_statistics.assert_awaited_once()


async def test_queued_refresh_does_not_start_after_paris_midnight(
    hass: HomeAssistant, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different meter can still hold the transport when the Paris date changes."""
    now = datetime(2026, 11, 16, 23, 59, tzinfo=PARIS_TZ)
    started = asyncio.Event()

    def local_time(_timezone):
        started.set()
        return now

    monkeypatch.setattr(
        "custom_components.linky.coordinator.dt_util", SimpleNamespace(now=local_time)
    )
    coordinator = LinkyDataUpdateCoordinator(hass, client, collect_export=False)
    budget = async_get_conso_budget(hass)

    async with budget.request("98765432109876"):
        refresh = asyncio.create_task(coordinator.async_refresh())
        await started.wait()
        now += timedelta(minutes=1)
    await refresh

    client.get_consumption_load_curve.assert_not_awaited()


async def test_slot_skips_startup_and_disabled_polling(
    hass: HomeAssistant, client: MagicMock, mock_config_entry: ConfigEntry
) -> None:
    """Skip busy startup and check the current entry preference at each slot."""
    coordinator = LinkyDataUpdateCoordinator(hass, client, config_entry=mock_config_entry)
    when = datetime(2024, 4, 1, 7, 45, tzinfo=UTC)
    cancel = MagicMock(return_value=None)
    with (
        patch(
            "custom_components.linky.coordinator.async_track_utc_time_change", return_value=cancel
        ) as track,
        patch("custom_components.linky.coordinator.async_get_conso_budget") as budget,
    ):
        coordinator.async_start()
        slot = track.call_args.args[1]
        hass.set_state(CoreState.starting)
        try:
            slot(when)
        finally:
            hass.set_state(CoreState.running)
        await asyncio.gather(*mock_config_entry._background_tasks)
        hass.config_entries.async_update_entry(mock_config_entry, pref_disable_polling=True)
        slot(when + _DAY)
        await asyncio.gather(*mock_config_entry._background_tasks)
        budget.assert_not_called()

    assert not client.mock_calls
    assert coordinator.data is None
    await mock_config_entry._async_process_on_unload(hass)


@pytest.mark.parametrize("historical", [False, True], ids=["refresh", "import"])
@pytest.mark.parametrize("daily_only", [False, True], ids=["curves", "daily"])
@pytest.mark.parametrize("collect_export", [False, True], ids=["consumption", "both"])
async def test_selected_datasets_write_statistics(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    historical: bool,
    daily_only: bool,
    collect_export: bool,
) -> None:
    """Read each selected direction once and write derived totals, including the DST boundary."""
    end = _START + _DAY if historical else dt_util.now(PARIS_TZ).date()
    start = _START if historical else end - 7 * _DAY
    coordinator = LinkyDataUpdateCoordinator(
        hass, client, collect_export=collect_export, daily_only=daily_only
    )
    selected = _ENDPOINTS[:2] if daily_only else _ENDPOINTS[2:4]
    for name in selected:
        getattr(client, name).side_effect = _daily if daily_only else _curve
    if not collect_export:
        selected = selected[:1]

    if historical:
        await coordinator.import_statistics(start, end)
        assert coordinator.data is None
    else:
        await coordinator.async_refresh()
        data = coordinator.data
        assert data.daily_consumption.unit == "Wh"
        assert data.daily_consumption.interval_reading[-1].date == end - _DAY
        assert (data.daily_production is not None) == collect_export
        assert (data.load_curve is not None) != daily_only
        assert (data.production_load_curve is not None) == (collect_export and not daily_only)
        if daily_only:
            assert data.daily_consumption == await _daily(start, end)
        else:
            hours = (paris_midnight(end) - paris_midnight(end - _DAY)) // _HOUR
            assert data.daily_consumption.interval_reading[-1].value == hours * 500

    for name in _ENDPOINTS:
        endpoint = getattr(client, name)
        if name in selected:
            endpoint.assert_awaited_once_with(start=start, end=end)
        else:
            endpoint.assert_not_awaited()

    first = paris_midnight(start)
    hours = (paris_midnight(end) - first) // _HOUR
    assert not historical or hours == 23
    expected = {}
    for direction in ("consumption", "production") if collect_export else ("consumption",):
        expected[direction, "daily"] = {
            paris_midnight(day): (
                1.001
                if daily_only
                else (paris_midnight(day + _DAY) - paris_midnight(day)) // _HOUR * 0.5
            )
            for day in (start + i * _DAY for i in range((end - start).days))
        }
        if not daily_only:
            expected[direction, "hourly"] = {first + i * _HOUR: 0.5 for i in range(hours)}
    write_statistics.assert_awaited_once_with(hass, _PRM, expected)


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("daily_only", [False, True], ids=["curves", "daily"])
async def test_provider_stop_responses_do_not_probe_or_retry(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    status: int,
    daily_only: bool,
) -> None:
    """Stop a batch at the first 403 or 429, in either source mode."""
    consumption = client.get_daily_consumption if daily_only else client.get_consumption_load_curve
    consumption.side_effect = APIError(f"HTTP {status}", status)
    coordinator = LinkyDataUpdateCoordinator(hass, client, daily_only=daily_only)

    await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert f"HTTP {status}" in str(coordinator.last_exception)
    assert coordinator.data is None
    consumption.assert_awaited_once()
    consumption.reset_mock()

    with pytest.raises(HomeAssistantError, match=f"HTTP {status}"):
        await coordinator.import_statistics(_START, _START + _DAY)
    consumption.assert_awaited_once()
    client.get_production_load_curve.assert_not_awaited()
    client.get_daily_production.assert_not_awaited()
    write_statistics.assert_not_awaited()


async def test_activation_limit_does_not_shorten_the_window(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Do not search for an available suffix when Conso rejects the requested range."""
    client.get_consumption_load_curve.side_effect = HistoryLimitError("Before activation", 400)
    coordinator = LinkyDataUpdateCoordinator(hass, client, collect_export=False)
    with pytest.raises(UpdateFailed, match="Before activation"):
        await coordinator._async_update_data()
    client.get_consumption_load_curve.assert_awaited_once()
    client.get_daily_consumption.assert_not_awaited()
    write_statistics.assert_not_awaited()


@pytest.mark.parametrize("daily_only", [False, True], ids=["curves", "daily"])
@pytest.mark.usefixtures("allow_every_claim")
async def test_forbidden_issue_requires_observed_failures_and_usable_recovery(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    mock_config_entry: ConfigEntry,
    daily_only: bool,
) -> None:
    """Track only the selected consumption endpoint; manual imports cannot change the issue."""
    coordinator = LinkyDataUpdateCoordinator(
        hass, client, config_entry=mock_config_entry, daily_only=daily_only
    )
    registry = ir.async_get(hass)
    key = (DOMAIN, f"conso_forbidden_{mock_config_entry.entry_id}")
    consumption = client.get_daily_consumption if daily_only else client.get_consumption_load_curve
    production = client.get_daily_production if daily_only else client.get_production_load_curve
    usable = _daily if daily_only else _curve
    forbidden = APIError("HTTP 403", 403)
    no_data = NoDataError("No measurements", 404)
    consumption.side_effect = usable
    production.side_effect = forbidden

    for _ in range(2):
        await coordinator.async_refresh()
    assert registry.async_get_issue(*key) is None

    production.side_effect = no_data
    for outcome in (forbidden, no_data, forbidden):
        consumption.side_effect = outcome
        await coordinator.async_refresh()
        assert registry.async_get_issue(*key) is None

    with pytest.raises(HomeAssistantError, match="HTTP 403"):
        await coordinator.import_statistics(_START, _START + _DAY)
    assert registry.async_get_issue(*key) is None

    await coordinator.async_refresh()
    issue = registry.async_get_issue(*key)
    assert issue is not None and issue.active
    assert issue.config_entry_id == mock_config_entry.entry_id
    assert issue.is_fixable is False
    assert issue.translation_key == "conso_forbidden"
    assert issue.translation_placeholders == {"meter": "1234"}

    invalid = MeteringData(_PRM, _START, _START + _DAY, [IntervalReading(_START, -1)], "Wh")
    for outcome in (no_data, APIError("Timed out"), ConsoBudgetExceeded("Paused"), None):
        consumption.side_effect = outcome
        consumption.return_value = invalid
        await coordinator.async_refresh()
        assert registry.async_get_issue(*key) is issue

    consumption.side_effect = usable
    await coordinator.import_statistics(_START, _START + _DAY)
    assert registry.async_get_issue(*key) is issue
    await coordinator.async_refresh()
    assert registry.async_get_issue(*key) is None


@pytest.mark.usefixtures("allow_every_claim")
async def test_refresh_only_reauthenticates_for_real_auth_errors(
    hass: HomeAssistant,
    client: MagicMock,
    write_statistics: AsyncMock,
    mock_config_entry: ConfigEntry,
) -> None:
    """No data, a provider failure and a genuine 401 have distinct effects."""
    coordinator = LinkyDataUpdateCoordinator(hass, client, config_entry=mock_config_entry)
    with patch.object(ConfigEntry, "async_start_reauth_if_available") as reauth:
        assert await coordinator._async_update_data() == LinkyData()
        client.get_consumption_load_curve.side_effect = APIError("Could not connect")
        await coordinator.async_refresh()
        assert isinstance(coordinator.last_exception, UpdateFailed)
        reauth.assert_not_called()

        client.get_consumption_load_curve.side_effect = AuthenticationError("Token rejected", 401)
        await coordinator.async_refresh()
        assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
        reauth.assert_called_once_with(hass)
    write_statistics.assert_not_awaited()


async def test_import_rejects_long_and_empty_windows_before_network(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Reject invalid and oversized requests without spending any calls."""
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    with pytest.raises(ServiceValidationError, match="at most 7 days"):
        await coordinator.import_statistics(_START, _START + 8 * _DAY)
    with pytest.raises(ServiceValidationError, match="increasing date range"):
        await coordinator.import_statistics(_START, _START)
    assert not client.mock_calls
    write_statistics.assert_not_awaited()


async def test_import_preserves_user_visible_budget_denial(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Return the original budget error when no series was imported."""
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    error = ConsoBudgetExceeded("Conso request limit reached")
    client.get_consumption_load_curve.side_effect = error
    with pytest.raises(ConsoBudgetError) as caught:
        await coordinator.import_statistics(_START, _START + _DAY)
    assert caught.value is error
    client.get_production_load_curve.assert_not_awaited()
    write_statistics.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        APIError("Forbidden", 403),
        APIError("Rate limit reached", 429),
        ConsoBudgetExceeded("Paused"),
    ],
    ids=["forbidden", "rate_limit", "budget"],
)
async def test_import_honors_partial_failure(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock, error: Exception
) -> None:
    """Commit measurements already fetched and report why production was skipped."""
    coordinator = LinkyDataUpdateCoordinator(hass, client)
    end = _START + _DAY
    with pytest.raises(ServiceValidationError, match="No Linky energy measurements"):
        await coordinator.import_statistics(_START, end)
    write_statistics.assert_not_awaited()
    client.reset_mock()

    client.get_consumption_load_curve.side_effect = _curve
    client.get_production_load_curve.side_effect = error
    with pytest.raises(
        HomeAssistantError, match=f"partially imported: production_load_curve.*{error}"
    ):
        await coordinator.import_statistics(_START, end)

    client.get_consumption_load_curve.assert_awaited_once_with(start=_START, end=end)
    client.get_production_load_curve.assert_awaited_once_with(start=_START, end=end)
    write_statistics.assert_awaited_once()
    assert write_statistics.await_args.args[2][("consumption", "daily")] == {
        paris_midnight(_START): 11.5
    }
    assert coordinator.data is None


async def test_import_reports_incomplete_curve_without_deriving_a_partial_day(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """Keep complete hours even when the Paris day cannot be derived."""
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
    with pytest.raises(HomeAssistantError, match="22 missing or invalid hours"):
        await coordinator.import_statistics(_START, end)
    assert write_statistics.await_args.args[2] == {
        ("consumption", "hourly"): {midnight: 0},
        ("consumption", "daily"): {},
    }


async def test_refresh_waits_for_import_write(
    hass: HomeAssistant, client: MagicMock, write_statistics: AsyncMock
) -> None:
    """A daily refresh cannot read the provider until an import finishes writing."""
    client.get_daily_consumption.side_effect = _daily
    coordinator = LinkyDataUpdateCoordinator(hass, client, daily_only=True)
    writing = asyncio.Event()
    committed = asyncio.Event()

    async def write(*_args) -> None:
        writing.set()
        await committed.wait()

    write_statistics.side_effect = write
    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        group.create_task(coordinator.import_statistics(_START, _START + _DAY))
        await writing.wait()
        group.create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        client.get_daily_consumption.assert_awaited_once()
        committed.set()
    assert client.get_daily_consumption.await_count == 2
