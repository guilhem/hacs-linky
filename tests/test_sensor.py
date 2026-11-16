"""Check the meter's latest published measurements."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.linky.api import IntervalReading, MeteringData
from custom_components.linky.const import CONF_COLLECT_EXPORT, CONF_DAILY_ONLY, DOMAIN
from custom_components.linky.coordinator import LinkyData

from .conftest import PRM


def _entity_id(hass: HomeAssistant, reading: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{PRM}_{reading}")
    assert entity_id is not None
    return entity_id


async def test_latest_daily_reading_is_not_a_cumulative_meter(
    hass: HomeAssistant, mock_linky_client: MagicMock, mock_config_entry, daily_consumption_data
) -> None:
    """Use measurement dates rather than response order."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_config_entry.runtime_data.async_set_updated_data(
        LinkyData(
            daily_consumption=replace(
                daily_consumption_data,
                interval_reading=list(reversed(daily_consumption_data.interval_reading)),
            )
        )
    )

    entity_id = _entity_id(hass, "daily_consumption")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "12100"
    assert state.attributes["unit_of_measurement"] == UnitOfEnergy.WATT_HOUR
    assert state.attributes["last_date"] == "2024-01-07"
    assert state.attributes["device_class"] == "energy"
    assert "state_class" not in state.attributes
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{PRM}_total_consumption_kwh")
        is None
    )

    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    assert (DOMAIN, PRM) in device.identifiers


async def test_curve_sensor_shows_measurement_time(
    hass: HomeAssistant, mock_linky_client: MagicMock, mock_config_entry, load_curve_data
) -> None:
    """Report active power in W and no longer expose a peak sensor."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_config_entry.runtime_data.async_set_updated_data(LinkyData(load_curve=load_curve_data))

    power = hass.states.get(_entity_id(hass, "current_power"))
    assert power is not None
    assert power.state == "520"
    assert power.attributes["unit_of_measurement"] == UnitOfPower.WATT
    assert power.attributes["last_date"] == "2024-01-07T01:00:00+01:00"

    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{PRM}_max_power") is None


async def test_empty_endpoint_is_unavailable(
    hass: HomeAssistant,
    mock_linky_client: MagicMock,
    mock_config_entry,
    daily_consumption_data,
    load_curve_data,
) -> None:
    """An empty curve does not affect daily readings or appear as zero power."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_config_entry.runtime_data.async_set_updated_data(
        LinkyData(
            daily_consumption=daily_consumption_data,
            load_curve=replace(load_curve_data, interval_reading=[]),
        )
    )

    power = hass.states.get(_entity_id(hass, "current_power"))
    assert power is not None and power.state == STATE_UNAVAILABLE
    daily = hass.states.get(_entity_id(hass, "daily_consumption"))
    assert daily is not None and daily.state == "12100"
    export = er.async_get(hass).async_get(_entity_id(hass, "daily_production"))
    assert export is not None and export.disabled


@pytest.mark.parametrize(
    ("options", "failed_update"),
    [
        pytest.param({}, False, id="empty-endpoint"),
        pytest.param({}, True, id="failed-refresh"),
        pytest.param({CONF_COLLECT_EXPORT: False}, False, id="export-disabled"),
        pytest.param({CONF_DAILY_ONLY: True}, False, id="daily-only"),
    ],
)
async def test_restore_until_first_result(
    hass: HomeAssistant,
    mock_linky_client: MagicMock,
    mock_config_entry,
    daily_consumption_data: MeteringData,
    load_curve_data: MeteringData,
    options: dict[str, bool],
    failed_update: bool,
) -> None:
    """Load HA's serialized sensor state, then replace it with the first update result."""
    registry = er.async_get(hass)
    for key in ("daily_production", "current_production_power"):
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{PRM}_{key}", config_entry=mock_config_entry
        )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    daily_id = _entity_id(hass, "daily_consumption")
    registry.async_update_entity_options(daily_id, "sensor", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    assert hass.states.get(daily_id).state == STATE_UNAVAILABLE

    mock_config_entry.runtime_data.async_set_updated_data(
        LinkyData(
            daily_consumption=daily_consumption_data,
            load_curve=load_curve_data,
            daily_production=daily_consumption_data,
            production_load_curve=load_curve_data,
        )
    )
    saved = hass.states.get(daily_id)
    assert saved is not None and float(saved.state) == 12.1
    saved_states = restore_state.async_get(hass)
    await saved_states.async_dump_states()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await saved_states.async_load()
    hass.config_entries.async_update_entry(mock_config_entry, options=options)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data
    assert coordinator.data is None
    mock_linky_client.get_daily_consumption.assert_not_awaited()
    mock_linky_client.get_consumption_load_curve.assert_not_awaited()
    mock_linky_client.get_daily_production.assert_not_awaited()
    mock_linky_client.get_production_load_curve.assert_not_awaited()

    restored = hass.states.get(daily_id)
    assert restored is not None and restored.state == saved.state
    assert restored.attributes["last_value"] == 12100
    assert restored.attributes["last_date"] == "2024-01-07"
    assert restored.attributes["quality"] == "BRUT"
    daily_only = options.get(CONF_DAILY_ONLY, False)
    export_disabled = not options.get(CONF_COLLECT_EXPORT, True)
    for key, expected, disabled in (
        ("current_power", "520", daily_only),
        ("daily_production", "12100", export_disabled),
        ("current_production_power", "520", daily_only or export_disabled),
    ):
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == (STATE_UNAVAILABLE if disabled else expected)

    if failed_update:
        coordinator.async_set_update_error(UpdateFailed("Failed to refresh"))
    else:
        coordinator.async_set_updated_data(LinkyData(load_curve=load_curve_data))
    state = hass.states.get(daily_id)
    assert state is not None and state.state == STATE_UNAVAILABLE
    assert "last_date" not in state.attributes
    assert "quality" not in state.attributes

    coordinator.async_set_updated_data(
        LinkyData(
            daily_consumption=replace(
                daily_consumption_data,
                interval_reading=[IntervalReading(daily_consumption_data.end, 0)],
                quality="CORRIGE",
            )
        )
    )
    state = hass.states.get(daily_id)
    assert state is not None and float(state.state) == 0
    assert state.attributes["last_date"] == daily_consumption_data.end.isoformat()
    assert state.attributes["quality"] == "CORRIGE"
