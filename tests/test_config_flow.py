"""Tests for the Linky config flow."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pylinky import InvalidTokenError, PRMAccessError

from custom_components.linky.const import CONF_PRM, DOMAIN


async def test_form_single_prm(
    hass: HomeAssistant,
    single_prm_token: str,
    mock_linky_client: MagicMock,
) -> None:
    """Test successful config flow with single PRM."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: single_prm_token},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Linky 12345678901234"
    assert result["data"] == {
        CONF_TOKEN: single_prm_token,
        CONF_PRM: "12345678901234",
    }
    assert result["result"].unique_id == "12345678901234"


async def test_form_multi_prm(
    hass: HomeAssistant,
    multi_prm_token: str,
    mock_linky_client_multi_prm: MagicMock,
) -> None:
    """Test config flow with multiple PRMs shows selection step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: multi_prm_token},
    )

    # Should show PRM selection step
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_prm"

    # Select a PRM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRM: "98765432109876"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Linky 98765432109876"
    assert result["data"][CONF_PRM] == "98765432109876"


async def test_form_invalid_token(hass: HomeAssistant) -> None:
    """Test error handling for invalid token."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.linky.config_flow.AsyncLinkyClient",
        side_effect=InvalidTokenError("Invalid token"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TOKEN: "invalid-token"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token"}


async def test_form_prm_access_denied(
    hass: HomeAssistant,
    multi_prm_token: str,
    mock_linky_client_multi_prm: MagicMock,
) -> None:
    """Test error handling for PRM access denied."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: multi_prm_token},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_prm"

    # Mock PRMAccessError for the selection step - use a valid PRM from the list
    with patch(
        "custom_components.linky.config_flow.AsyncLinkyClient",
        side_effect=PRMAccessError("98765432109876"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRM: "98765432109876"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "prm_access_denied"}


async def test_form_already_configured(
    hass: HomeAssistant,
    single_prm_token: str,
    mock_linky_client: MagicMock,
    mock_config_entry,
) -> None:
    """Test that we abort if already configured."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: single_prm_token},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_form_unknown_error(hass: HomeAssistant) -> None:
    """Test error handling for unknown errors."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.linky.config_flow.AsyncLinkyClient",
        side_effect=Exception("Unknown error"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TOKEN: "some-token"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
