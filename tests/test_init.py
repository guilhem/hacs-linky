"""Tests for Linky integration setup and actions."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.linky import SERVICE_IMPORT_HISTORICAL_DATA
from custom_components.linky.api import APIError, AuthenticationError, NoDataError
from custom_components.linky.const import CONF_COLLECT_EXPORT, CONF_SCAN_INTERVAL, DOMAIN

pytestmark = pytest.mark.usefixtures("recorder_mock")


async def test_setup_entry(
    hass: HomeAssistant, mock_linky_client, mock_config_entry, mock_http_session
) -> None:
    """Unload the entry without closing Home Assistant's shared session."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.client is mock_linky_client
    assert mock_config_entry.runtime_data.update_interval.total_seconds() == 12 * 3600

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA)
    mock_http_session.close.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "expected_state", "needs_reauth"),
    [
        (AuthenticationError("Bearer rejected", 401), ConfigEntryState.SETUP_ERROR, True),
        (APIError("Could not connect to Conso API"), ConfigEntryState.SETUP_RETRY, False),
        (NoDataError("Enedis returned no measurements", 404), ConfigEntryState.LOADED, False),
    ],
)
async def test_first_refresh_provider_response(
    hass: HomeAssistant, mock_linky_client, mock_config_entry, failure, expected_state, needs_reauth
) -> None:
    """Keep a meter with no readings and distinguish outages from rejected credentials."""
    for method in (
        mock_linky_client.get_daily_consumption,
        mock_linky_client.get_consumption_load_curve,
        mock_linky_client.get_max_power,
        mock_linky_client.get_daily_production,
        mock_linky_client.get_production_load_curve,
    ):
        method.side_effect = failure

    loaded = await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert loaded is (expected_state is ConfigEntryState.LOADED)
    assert mock_config_entry.state is expected_state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows) is needs_reauth


async def test_reload_from_options(
    hass: HomeAssistant, mock_linky_client, mock_config_entry
) -> None:
    """Saving options reloads the entry with the new interval and export setting."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    previous_coordinator = mock_config_entry.runtime_data
    mock_linky_client.get_daily_production.assert_awaited_once()
    mock_linky_client.get_production_load_curve.assert_awaited_once()
    mock_linky_client.reset_mock()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 8, CONF_COLLECT_EXPORT: False}
    )
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not previous_coordinator
    assert mock_config_entry.runtime_data.update_interval.total_seconds() == 8 * 3600
    mock_linky_client.get_daily_consumption.assert_awaited_once()
    mock_linky_client.get_daily_production.assert_not_awaited()
    mock_linky_client.get_production_load_curve.assert_not_awaited()


async def test_import_targets_selected_loaded_meter(
    hass: HomeAssistant, mock_linky_client, mock_config_entry, multi_prm_token: str, add_linky_entry
) -> None:
    """Dispatch by ID, including after one meter unloads."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    second = await add_linky_entry("98765432109876", multi_prm_token)
    second_client = MagicMock(
        prm="98765432109876",
        get_daily_consumption=AsyncMock(return_value=None),
        get_daily_production=AsyncMock(return_value=None),
        get_consumption_load_curve=AsyncMock(return_value=None),
        get_production_load_curve=AsyncMock(return_value=None),
        get_max_power=AsyncMock(return_value=None),
    )
    with patch("custom_components.linky.LinkyClient", return_value=second_client):
        assert await hass.config_entries.async_setup(second.entry_id)

    async def call(entry_id: str) -> None:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {"config_entry_id": entry_id, "start_date": "2026-10-01", "end_date": "2026-10-02"},
            blocking=True,
        )

    with (
        patch.object(mock_config_entry.runtime_data, "import_statistics") as import_first,
        patch.object(second.runtime_data, "import_statistics") as import_second,
    ):
        await call(second.entry_id)
        import_second.assert_awaited_once_with(date(2026, 10, 1), date(2026, 10, 2))
        import_first.assert_not_awaited()

        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        with pytest.raises(ServiceValidationError, match="Select a loaded Linky meter"):
            await call(mock_config_entry.entry_id)
        with pytest.raises(ServiceValidationError, match="Select a loaded Linky meter"):
            await call("nonexistent")
        await call(second.entry_id)
        assert import_second.await_count == 2
        import_first.assert_not_awaited()


async def test_import_dates_permissions_and_failure(
    hass: HomeAssistant, hass_read_only_user, mock_linky_client, mock_config_entry
) -> None:
    """Use Paris dates, reject invalid and unauthorized calls, and propagate failures."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)

    async def call(data: dict, context: Context | None = None) -> None:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            data,
            blocking=True,
            context=context,
        )

    data = {"config_entry_id": mock_config_entry.entry_id, "start_date": "2026-01-02"}
    with (
        patch.object(
            mock_config_entry.runtime_data, "import_statistics", new_callable=AsyncMock
        ) as import_statistics,
        freeze_time("2026-01-02 23:30:00", real_asyncio=True),
    ):
        with pytest.raises(Unauthorized):
            await call(data, Context(user_id=hass_read_only_user.id))
        with pytest.raises(ServiceValidationError, match="Start date must be before end date"):
            await call(data | {"end_date": "2026-01-02"})
        with pytest.raises(ServiceValidationError, match="End date cannot be after today"):
            await call(data | {"end_date": "2026-01-04"})
        import_statistics.assert_not_awaited()

        await call(data)
        import_statistics.assert_awaited_once_with(date(2026, 1, 2), date(2026, 1, 3))

        import_statistics.side_effect = ServiceValidationError("No readings in the selected range")
        with pytest.raises(ServiceValidationError, match="No readings"):
            await call(data)


async def test_unload_interrupts_import(
    hass: HomeAssistant, mock_linky_client, mock_config_entry
) -> None:
    """Do not let an import keep running with the unloaded entry's coordinator."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    started = asyncio.Event()

    async def pending_import(start: date, end: date) -> None:
        started.set()
        await asyncio.Future()

    with patch.object(mock_config_entry.runtime_data, "import_statistics", pending_import):
        action = asyncio.create_task(
            hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_HISTORICAL_DATA,
                {"config_entry_id": mock_config_entry.entry_id, "start_date": "2026-01-02"},
                blocking=True,
            )
        )
        async with asyncio.timeout(5):
            await started.wait()
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        with pytest.raises(ServiceValidationError, match="unloaded during import"):
            await action
