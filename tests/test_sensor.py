"""Check the meter's latest published measurements."""

from dataclasses import replace
from unittest.mock import MagicMock

from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.linky.const import DOMAIN


def _entity_id(hass: HomeAssistant, reading: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"12345678901234_{reading}"
    )
    assert entity_id is not None
    return entity_id


async def test_latest_daily_reading_is_not_a_cumulative_meter(
    hass: HomeAssistant, mock_linky_client: MagicMock, mock_config_entry, daily_consumption_data
) -> None:
    """Use measurement dates rather than response order."""
    mock_linky_client.get_daily_consumption.return_value = replace(
        daily_consumption_data,
        interval_reading=list(reversed(daily_consumption_data.interval_reading)),
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = _entity_id(hass, "daily_consumption")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "12100"
    assert state.attributes["unit_of_measurement"] == UnitOfEnergy.WATT_HOUR
    assert state.attributes["last_date"] == "2024-01-07"
    assert state.attributes["device_class"] == "energy"
    assert "state_class" not in state.attributes
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, "12345678901234_total_consumption_kwh"
        )
        is None
    )

    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    assert (DOMAIN, "12345678901234") in device.identifiers


async def test_power_sensors_show_measurement_time(
    hass: HomeAssistant, mock_linky_client: MagicMock, mock_config_entry
) -> None:
    """Report active power in W and apparent peak power in VA."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    power = hass.states.get(_entity_id(hass, "current_power"))
    assert power is not None
    assert power.state == "520"
    assert power.attributes["unit_of_measurement"] == UnitOfPower.WATT
    assert power.attributes["last_date"] == "2024-01-07T01:00:00+01:00"

    peak = hass.states.get(_entity_id(hass, "max_power"))
    assert peak is not None
    assert peak.state == "5800"
    assert peak.attributes["unit_of_measurement"] == "VA"
    assert peak.attributes["device_class"] == "apparent_power"


async def test_empty_endpoint_is_unavailable(
    hass: HomeAssistant, mock_linky_client: MagicMock, mock_config_entry, load_curve_data
) -> None:
    """An empty curve does not affect daily readings or appear as zero power."""
    mock_linky_client.get_consumption_load_curve.return_value = replace(
        load_curve_data, interval_reading=[]
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    power = hass.states.get(_entity_id(hass, "current_power"))
    assert power is not None and power.state == STATE_UNAVAILABLE
    daily = hass.states.get(_entity_id(hass, "daily_consumption"))
    assert daily is not None and daily.state == "12100"
    export = er.async_get(hass).async_get(_entity_id(hass, "daily_production"))
    assert export is not None and export.disabled
