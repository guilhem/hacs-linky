"""Tests for Linky integration setup and actions."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from custom_components.linky import SERVICE_IMPORT_HISTORICAL_DATA
from custom_components.linky.api import AuthenticationError
from custom_components.linky.const import CONF_COLLECT_EXPORT, CONF_DAILY_ONLY, DOMAIN

pytestmark = pytest.mark.usefixtures("recorder_mock")


async def test_setup_entry(
    hass: HomeAssistant, mock_linky_client, mock_config_entry, mock_http_session
) -> None:
    """Unload the entry without closing Home Assistant's shared session."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.client is mock_linky_client
    mock_linky_client.get_consumption_load_curve.assert_not_awaited()
    mock_linky_client.get_daily_consumption.assert_not_awaited()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA)
    mock_http_session.close.assert_not_called()


async def test_scheduled_refresh_requests_reauth_for_a_rejected_token(
    hass: HomeAssistant, mock_linky_client, mock_config_entry
) -> None:
    """A real 401 during the scheduled refresh still asks for a new token."""
    mock_linky_client.get_consumption_load_curve.side_effect = AuthenticationError(
        "Bearer rejected", 401
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_reload_from_options(
    hass: HomeAssistant, mock_linky_client, mock_config_entry
) -> None:
    """Saving collection options reloads the entry without sending a request."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    previous_coordinator = mock_config_entry.runtime_data

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DAILY_ONLY: True, CONF_COLLECT_EXPORT: False}
    )
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not previous_coordinator
    mock_linky_client.get_daily_consumption.assert_not_awaited()
    await mock_config_entry.runtime_data.async_refresh()
    mock_linky_client.get_daily_consumption.assert_awaited_once()
    mock_linky_client.get_consumption_load_curve.assert_not_awaited()
    mock_linky_client.get_daily_production.assert_not_awaited()
    mock_linky_client.get_production_load_curve.assert_not_awaited()


async def test_refresh_import_and_reconfigure_share_one_request_budget(
    hass: HomeAssistant, mock_config_entry, single_prm_token: str, mock_http_session: MagicMock
) -> None:
    """All three callers share the same cap, and restarts don't refetch before the timer."""
    entry = mock_config_entry
    hass.config_entries.async_update_entry(
        entry, options={CONF_DAILY_ONLY: True, CONF_COLLECT_EXPORT: False}
    )
    reply = mock_http_session.get.return_value.__aenter__.return_value
    reply.status = 200
    reply.headers = {}
    reply.json = AsyncMock(
        return_value={
            "idPrm": entry.data["prm"],
            "periode": {"dateDebut": "2026-01-02", "dateFin": "2026-01-03"},
            "grandeur": [
                {
                    "grandeurMetier": "CONS",
                    "grandeurPhysique": "EA",
                    "unite": "Wh",
                    "points": [{"d": "2026-01-02", "v": "1200"}],
                }
            ],
        }
    )

    async def import_day() -> None:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            {"config_entry_id": entry.entry_id, "start_date": "2026-01-02"},
            blocking=True,
        )

    with freeze_time("2026-01-03 10:00:00", real_asyncio=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        mock_http_session.get.assert_not_called()
        await entry.runtime_data.async_refresh()
        await entry.runtime_data.async_refresh()
        mock_http_session.get.assert_called_once()

        await import_day()
        assert mock_http_session.get.call_count == 2

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        with patch.object(hass.config_entries, "async_schedule_reload"):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"token": single_prm_token}
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert mock_http_session.get.call_count == 3

        with pytest.raises(HomeAssistantError, match="(?i)budget|limit"):
            await import_day()
        assert mock_http_session.get.call_count == 3


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
