"""Expose the latest published Linky readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfApparentPower, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LinkyConfigEntry
from .api import IntervalReading, MeteringData
from .const import (
    ATTR_LAST_DATE,
    ATTR_LAST_VALUE,
    ATTR_QUALITY,
    ATTR_USAGE_POINT_ID,
    DOMAIN,
    PARIS_TZ,
)
from .coordinator import LinkyDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class LinkySensorDescription(SensorEntityDescription):
    """Describe a published meter reading."""

    data_key: str


SENSORS = (
    LinkySensorDescription(
        key="daily_consumption",
        translation_key="daily_consumption",
        data_key="daily_consumption",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    LinkySensorDescription(
        key="current_power",
        translation_key="current_power",
        data_key="load_curve",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
    ),
    LinkySensorDescription(
        key="max_power",
        translation_key="max_power",
        data_key="max_power",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
    ),
    LinkySensorDescription(
        key="daily_production",
        translation_key="daily_production",
        data_key="daily_production",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        entity_registry_enabled_default=False,
    ),
    LinkySensorDescription(
        key="current_production_power",
        translation_key="current_production_power",
        data_key="production_load_curve",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        entity_registry_enabled_default=False,
    ),
)


def _reading_time(reading: IntervalReading) -> datetime:
    """Interpret timestamps without an offset as French local time."""
    value = reading.date
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=PARIS_TZ)
    return datetime.combine(value, time.min, PARIS_TZ)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinkyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the meter's sensors."""
    async_add_entities(LinkySensor(entry, description) for description in SENSORS)


class LinkySensor(CoordinatorEntity[LinkyDataUpdateCoordinator], SensorEntity):
    """A snapshot of a single measurement series."""

    entity_description: LinkySensorDescription
    _attr_has_entity_name = True

    def __init__(self, entry: LinkyConfigEntry, description: LinkySensorDescription) -> None:
        """Set up the sensor and associate it with the meter."""
        super().__init__(entry.runtime_data)
        self.entity_description = description
        prm = self.coordinator.client.prm
        self._attr_unique_id = f"{prm}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, prm)},
            name=f"Linky {prm}",
            manufacturer="Enedis",
            model="Linky",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _meter_data(self) -> MeteringData | None:
        return getattr(self.coordinator.data, self.entity_description.data_key, None)

    def _latest_reading(self, data: MeteringData) -> IntervalReading | None:
        return max(
            data.interval_reading,
            key=lambda reading: _reading_time(reading).timestamp(),
            default=None,
        )

    @property
    def available(self) -> bool:
        """Report availability only when the endpoint has a reading."""
        return super().available and bool((data := self._meter_data()) and data.interval_reading)

    @property
    def native_value(self) -> int | float | None:
        """Return the latest reading rather than depending on API order."""
        if (data := self._meter_data()) and (reading := self._latest_reading(data)):
            return reading.value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show when Conso measured this value."""
        if (data := self._meter_data()) and (reading := self._latest_reading(data)):
            reading_date = reading.date
            if isinstance(reading_date, datetime):
                reading_date = _reading_time(reading).astimezone(PARIS_TZ)
            return {
                ATTR_LAST_DATE: reading_date.isoformat(),
                ATTR_LAST_VALUE: reading.value,
                ATTR_QUALITY: data.quality,
                ATTR_USAGE_POINT_ID: data.usage_point_id,
            }
        return None
