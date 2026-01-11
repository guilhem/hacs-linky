"""Config flow for Linky integration."""

# pylint: disable=abstract-method

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pylinky import AsyncLinkyClient, InvalidTokenError, PRMAccessError

from .const import (
    CONF_PRM,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_SCAN_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)


class LinkyOptionsFlow(OptionsFlow):
    """Handle options flow for Linky."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_HOURS
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_interval,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_HOURS,
                            max=MAX_SCAN_INTERVAL_HOURS,
                            step=1,
                            mode=NumberSelectorMode.SLIDER,
                            unit_of_measurement="h",
                        )
                    ),
                }
            ),
        )


class LinkyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Linky."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LinkyOptionsFlow:  # noqa: ARG004
        """Get the options flow for this handler."""
        return LinkyOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._token: str | None = None
        self._prms: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
