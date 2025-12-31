"""Sensor platform for Linky integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfApparentPower, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LinkyConfigEntry
from .const import ATTR_LAST_DATE, ATTR_LAST_VALUE, ATTR_QUALITY, ATTR_USAGE_POINT_ID, DOMAIN
from .coordinator import LinkyData, LinkyDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class LinkySensorEntityDescription(SensorEntityDescription):
    """Describes a Linky sensor entity."""

    value_fn: Callable[[LinkyData], int | float | None]
    available_fn: Callable[[LinkyData], bool]
    extra_state_fn: Callable[[LinkyData], dict[str, Any]] | None = None
    last_reset_fn: Callable[[LinkyData], datetime | None] | None = None


def _get_last_reading_attrs(data: LinkyData, attr: str) -> dict[str, Any]:
    """Get extra state attributes for the last reading."""
    metering_data = getattr(data, attr)
    if metering_data is None or not metering_data.interval_reading:
        return {}
    last = metering_data.interval_reading[-1]
    return {
        ATTR_USAGE_POINT_ID: metering_data.usage_point_id,
        ATTR_QUALITY: metering_data.quality,
        ATTR_LAST_VALUE: last.value,
        ATTR_LAST_DATE: last.date.isoformat(),
    }


SENSOR_DESCRIPTIONS: tuple[LinkySensorEntityDescription, ...] = (
    LinkySensorEntityDescription(
        key="daily_consumption",
        translation_key="daily_consumption",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (
            data.daily_consumption.interval_reading[-1].value
            if data.daily_consumption and data.daily_consumption.interval_reading
            else None
        ),
        available_fn=lambda data: data.daily_consumption is not None,
        extra_state_fn=lambda data: _get_last_reading_attrs(data, "daily_consumption"),
        last_reset_fn=lambda data: (
            datetime.combine(
                data.daily_consumption.interval_reading[-1].date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            if data.daily_consumption and data.daily_consumption.interval_reading
            else None
        ),
    ),
    LinkySensorEntityDescription(
        key="total_consumption_week",
        translation_key="total_consumption_week",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (
            data.daily_consumption.total if data.daily_consumption else None
        ),
        available_fn=lambda data: data.daily_consumption is not None,
        last_reset_fn=lambda data: (
            datetime.combine(data.daily_consumption.start, datetime.min.time(), tzinfo=timezone.utc)
            if data.daily_consumption
            else None
        ),
    ),
    LinkySensorEntityDescription(
        key="current_power",
        translation_key="current_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.load_curve.interval_reading[-1].value
            if data.load_curve and data.load_curve.interval_reading
            else None
        ),
        available_fn=lambda data: data.load_curve is not None,
    ),
    LinkySensorEntityDescription(
        key="max_power",
        translation_key="max_power",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.max_power.interval_reading[-1].value
            if data.max_power and data.max_power.interval_reading
            else None
        ),
        available_fn=lambda data: data.max_power is not None,
        extra_state_fn=lambda data: _get_last_reading_attrs(data, "max_power"),
    ),
    LinkySensorEntityDescription(
        key="daily_production",
        translation_key="daily_production",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (
            data.daily_production.interval_reading[-1].value
            if data.daily_production and data.daily_production.interval_reading
            else None
        ),
        available_fn=lambda data: data.daily_production is not None,
        extra_state_fn=lambda data: _get_last_reading_attrs(data, "daily_production"),
        last_reset_fn=lambda data: (
            datetime.combine(
                data.daily_production.interval_reading[-1].date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            if data.daily_production and data.daily_production.interval_reading
            else None
        ),
        entity_registry_enabled_default=False,
    ),
    LinkySensorEntityDescription(
        key="current_production_power",
        translation_key="current_production_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.production_load_curve.interval_reading[-1].value
            if data.production_load_curve and data.production_load_curve.interval_reading
            else None
        ),
        available_fn=lambda data: data.production_load_curve is not None,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinkyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Linky sensors based on a config entry."""
    coordinator = entry.runtime_data

    async_add_entities(
        LinkySensor(coordinator, description, entry)
        for description in SENSOR_DESCRIPTIONS
    )


class LinkySensor(CoordinatorEntity[LinkyDataUpdateCoordinator], SensorEntity):
    """Representation of a Linky sensor."""

    entity_description: LinkySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LinkyDataUpdateCoordinator,
        description: LinkySensorEntityDescription,
        entry: LinkyConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=f"Linky {entry.unique_id}",
            manufacturer="Enedis",
            model="Linky",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self.coordinator.data is None:
            return False
        return self.entity_description.available_fn(self.coordinator.data)

    @property
    def native_value(self) -> int | float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return None
        if self.entity_description.extra_state_fn is None:
            return None
        return self.entity_description.extra_state_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        """Return the time when the sensor was last reset."""
        if self.coordinator.data is None:
            return None
        if self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn(self.coordinator.data)
