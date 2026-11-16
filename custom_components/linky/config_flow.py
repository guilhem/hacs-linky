"""Config flow for Linky integration."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
from homeassistant.util import dt as dt_util

from .api import (
    APIError,
    AuthenticationError,
    HistoryLimitError,
    LinkyClient,
    NoDataError,
    PRMAccessError,
    fetch_recent,
)
from .const import (
    CONF_PRM,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_SCAN_INTERVAL_HOURS,
    PARIS_TZ,
)

_LOGGER = logging.getLogger(__name__)
TOKEN_SCHEMA = vol.Schema(
    {vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)


class LinkyOptionsFlow(OptionsFlowWithReload):
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

    async def _async_check_token(
        self, token: str, prm: str | None = None
    ) -> tuple[list[str], str | None]:
        """Discover meters and check API access once the meter is known."""
        try:
            client = LinkyClient(async_get_clientsession(self.hass), token, prm=prm)
            if prm is not None or len(client.prms) == 1:
                end = dt_util.now(PARIS_TZ).date()
                # Both responses confirm access; a new meter may have no completed day yet.
                with suppress(NoDataError, HistoryLimitError):
                    await fetch_recent(client.get_daily_consumption, end - timedelta(days=3), end)
            return client.prms, None
        except AuthenticationError:
            return [], "invalid_token"
        except PRMAccessError:
            return [], "prm_access_denied"
        except APIError as err:
            _LOGGER.warning("Could not verify access to the Linky meter: %s", err)
            return [], "cannot_connect"
        except RuntimeError, ValueError:
            _LOGGER.exception("Unexpected error during Linky token validation")
            return [], "unknown"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial token input."""
        errors = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            prms, error = await self._async_check_token(token)
            if error is None:
                self._token, self._prms = token, prms
                if len(prms) == 1:
                    return await self._create_entry(token, prms[0])
                return await self.async_step_select_prm()
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"conso_url": "https://conso.boris.sh"},
        )

    async def async_step_select_prm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle meter selection when multiple meters are available."""
        assert self._token is not None
        errors = {}
        if user_input is not None:
            prm = user_input[CONF_PRM]
            _, error = await self._async_check_token(self._token, prm)
            if error is None:
                return await self._create_entry(self._token, prm)
            errors["base"] = error

        return self.async_show_form(
            step_id="select_prm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRM): SelectSelector(
                        SelectSelectorConfig(options=self._prms, mode=SelectSelectorMode.DROPDOWN)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for the existing meter."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the replacement token against the existing meter and reload."""
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            _, error = await self._async_check_token(token, entry.data[CONF_PRM])
            if error is None:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={
                "conso_url": "https://conso.boris.sh",
                "prm": entry.data[CONF_PRM],
            },
        )

    async def _create_entry(self, token: str, prm: str) -> ConfigFlowResult:
        """Create a config entry for the selected meter."""
        await self.async_set_unique_id(prm)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Linky {prm}", data={CONF_TOKEN: token, CONF_PRM: prm}
        )
