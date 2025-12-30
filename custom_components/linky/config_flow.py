"""Config flow for Linky integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pylinky import AsyncLinkyClient, InvalidTokenError, PRMAccessError

from .const import CONF_PRM, DOMAIN

_LOGGER = logging.getLogger(__name__)


class LinkyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Linky."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._token: str | None = None
        self._prms: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - token input."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_TOKEN]

            try:
                client = AsyncLinkyClient(token=token)
                self._token = token
                self._prms = client.prms

                if len(self._prms) == 1:
                    # Single PRM, skip selection
                    return await self._create_entry(self._prms[0])

                # Multiple PRMs, go to selection step
                return await self.async_step_select_prm()

            except InvalidTokenError:
                errors["base"] = "invalid_token"
            except AbortFlow:
                raise
            except (OSError, RuntimeError, ValueError) as err:
                _LOGGER.exception("Unexpected error during token validation: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "conso_url": "https://conso.boris.sh",
            },
        )

    async def async_step_select_prm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle PRM selection when multiple PRMs are available."""
        errors: dict[str, str] = {}

        if user_input is not None:
            prm = user_input[CONF_PRM]

            try:
                # Validate PRM access
                if self._token is None:
                    errors["base"] = "unknown"
                else:
                    AsyncLinkyClient(token=self._token, prm=prm)
                    return await self._create_entry(prm)
            except PRMAccessError:
                errors["base"] = "prm_access_denied"
            except (OSError, RuntimeError, ValueError) as err:
                _LOGGER.exception("Unexpected error during PRM validation: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="select_prm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRM): SelectSelector(
                        SelectSelectorConfig(
                            options=self._prms,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def _create_entry(self, prm: str) -> ConfigFlowResult:
        """Create a config entry for the given PRM."""
        # Check if this PRM is already configured
        await self.async_set_unique_id(prm)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Linky {prm}",
            data={
                CONF_TOKEN: self._token,
                CONF_PRM: prm,
            },
        )
