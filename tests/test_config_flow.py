"""Tests for the Linky config and reauthentication flows."""

import json
from asyncio import IncompleteReadError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from freezegun import freeze_time
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.linky.api import PRMAccessError
from custom_components.linky.const import CONF_PRM, DOMAIN


def _mock_error_reply(reply: MagicMock, status: int, body: dict | None) -> None:
    """Model the content type and end of an HTTP error response."""
    reply.status = status
    reply.headers = {}
    reply.content_type = "application/json" if body is not None else "text/plain"
    payload = json.dumps(body).encode() if body is not None else b""
    reply.content_length = len(payload)
    reply.content.readexactly = AsyncMock(
        side_effect=IncompleteReadError(payload, len(payload) + 1)
    )


@pytest.fixture(autouse=True)
def skip_entry_setup(hass: HomeAssistant):
    """Keep flow validation calls separate from the new entry's first poll."""
    with (
        patch.object(hass.config_entries, "async_setup", return_value=True),
        patch("homeassistant.config_entries.async_process_deps_reqs"),
    ):
        yield


async def test_form_single_prm(
    hass: HomeAssistant, single_prm_token: str, mock_linky_client
) -> None:
    """Validate access against the API before creating an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: single_prm_token}
    )

    mock_linky_client.get_daily_consumption.assert_awaited_once()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_TOKEN: single_prm_token, CONF_PRM: "12345678901234"}
    assert result["result"].unique_id == "12345678901234"


async def test_form_multi_prm(
    hass: HomeAssistant, multi_prm_token: str, mock_linky_client_multi_prm
) -> None:
    """Probe the selected PRM rather than assuming the first meter is the target."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: multi_prm_token}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_prm"
    mock_linky_client_multi_prm.get_daily_consumption.assert_not_awaited()

    prm = "98765432109876"
    with patch(
        "custom_components.linky.config_flow.LinkyClient",
        return_value=mock_linky_client_multi_prm,
    ) as client_factory:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRM: prm})

    assert client_factory.call_args.kwargs["prm"] == prm
    mock_linky_client_multi_prm.get_daily_consumption.assert_awaited_once()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRM] == prm
    assert result["result"].unique_id == prm


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"status": 401, "json": {"message": "Bearer rejected"}}, "invalid_token"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"status": 503}, "cannot_connect"),
        ({"status": 404}, "cannot_connect"),
        (
            {
                "status": 403,
                "json": {
                    "status": 403,
                    "message": "The Enedis API returned an error",
                    "error": {"error": "ADAM-ERR0123", "error_description": "private meter detail"},
                },
            },
            "cannot_connect",
        ),
        (
            {
                "status": 404,
                "json": {
                    "message": "The Enedis API returned an error",
                    "error": {"error": "no_data_found"},
                },
            },
            None,
        ),
    ],
    ids=["rejected-token", "timeout", "server-error", "unknown-route", "forbidden", "no-readings"],
)
async def test_form_provider_response(
    hass: HomeAssistant, single_prm_token: str, response, error, caplog: pytest.LogCaptureFixture
) -> None:
    """Distinguish rejected credentials, outages, and a valid meter without data."""
    session = MagicMock(spec=ClientSession)
    request = session.get.return_value
    if "exc" in response:
        request.__aenter__.side_effect = response["exc"]
    else:
        reply = request.__aenter__.return_value
        _mock_error_reply(reply, response["status"], response.get("json"))

    with (
        patch(
            "custom_components.linky.config_flow.async_get_clientsession",
            return_value=session,
        ),
        freeze_time("2026-01-02 23:30:00", real_asyncio=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_TOKEN: single_prm_token},
        )

    session.get.assert_called_once()
    args, kwargs = session.get.call_args
    assert args == ("https://conso.boris.sh/api/consommation_quotidienne",)
    assert kwargs["params"] == {
        "pointId": "12345678901234",
        "dateDebut": "2025-12-31",
        "dateFin": "2026-01-03",
    }
    assert kwargs["headers"]["Authorization"] == f"Bearer {single_prm_token}"
    if error is None:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].unique_id == "12345678901234"
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}

    if response.get("status") == 403:
        assert "HTTP 403" in caplog.text
        assert "ADAM-ERR0123" in caplog.text
        assert "private meter detail" not in caplog.text
        assert single_prm_token not in caplog.text


async def test_form_invalid_token(hass: HomeAssistant) -> None:
    """Reject a malformed JWT before attempting a provider request."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_TOKEN: "invalid-token"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token"}


async def test_form_already_configured(
    hass: HomeAssistant, single_prm_token: str, mock_linky_client, mock_config_entry
) -> None:
    """Do not create a second entry for an existing meter."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_TOKEN: single_prm_token}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_keeps_meter_and_reloads(
    hass: HomeAssistant, multi_prm_token: str, mock_linky_client, mock_config_entry
) -> None:
    """Keep the original entry and check its meter before saving a replacement token."""
    original_data = dict(mock_config_entry.data)
    prm = original_data[CONF_PRM]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": mock_config_entry.entry_id},
        data=original_data,
    )
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["prm"] == prm

    with patch(
        "custom_components.linky.config_flow.LinkyClient", side_effect=PRMAccessError(prm)
    ) as denied_client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: multi_prm_token}
        )
    assert denied_client.call_args.kwargs["prm"] == prm
    assert result["errors"] == {"base": "prm_access_denied"}
    assert mock_config_entry.data == original_data

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: multi_prm_token}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == original_data | {CONF_TOKEN: multi_prm_token}
    assert mock_config_entry.unique_id == prm
    reload_entry.assert_called_once_with(mock_config_entry.entry_id)
    mock_linky_client.get_daily_consumption.assert_awaited_once()


async def test_newly_activated_meter_can_be_configured(
    hass: HomeAssistant, single_prm_token: str
) -> None:
    """A verified token can be saved before the first full day of meter data."""
    session = MagicMock(spec=ClientSession)
    reply = session.get.return_value.__aenter__.return_value
    _mock_error_reply(
        reply,
        400,
        {
            "status": 400,
            "message": "The Enedis API returned an error",
            "error": {"error": "ADAM-ERR0123"},
        },
    )
    with (
        patch("custom_components.linky.config_flow.async_get_clientsession", return_value=session),
        freeze_time("2026-01-02 23:30:00", real_asyncio=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_TOKEN: single_prm_token},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [call.kwargs["params"]["dateDebut"] for call in session.get.call_args_list] == [
        "2025-12-31",
        "2026-01-02",
    ]
