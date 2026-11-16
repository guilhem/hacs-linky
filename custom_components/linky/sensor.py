"""Expose the latest published Linky readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
)
from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LinkyConfigEntry
from .api import IntervalReading
from .const import (
    ATTR_LAST_DATE,
    ATTR_LAST_VALUE,
    ATTR_QUALITY,
    ATTR_USAGE_POINT_ID,
    CONF_COLLECT_EXPORT,
    CONF_DAILY_ONLY,
    DEFAULT_COLLECT_EXPORT,
    DOMAIN,
    PARIS_TZ,
)
from .coordinator import LinkyDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class LinkySensorDescription(SensorEntityDescription):
    """Describe a published meter reading."""

    data_key: str
    requires_export: bool = False


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
        key="daily_production",
        translation_key="daily_production",
        data_key="daily_production",
        requires_export=True,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        entity_registry_enabled_default=False,
    ),
    LinkySensorDescription(
        key="current_production_power",
        translation_key="current_production_power",
        data_key="production_load_curve",
        requires_export=True,
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


class LinkySensor(CoordinatorEntity[LinkyDataUpdateCoordinator], RestoreSensor):
    """A snapshot of a single measurement series."""

    entity_description: LinkySensorDescription
    _attr_has_entity_name = True

    def __init__(self, entry: LinkyConfigEntry, description: LinkySensorDescription) -> None:
        """Set up the sensor and associate it with the meter."""
        super().__init__(entry.runtime_data)
        self.entity_description = description
        self._collection_enabled = (
            not description.requires_export
            or entry.options.get(CONF_COLLECT_EXPORT, DEFAULT_COLLECT_EXPORT)
        ) and (
            description.device_class != SensorDeviceClass.POWER
            or not entry.options.get(CONF_DAILY_ONLY, False)
        )
        prm = self.coordinator.client.prm
        self._attr_unique_id = f"{prm}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, prm)},
            name=f"Linky {prm}",
            manufacturer="Enedis",
            model="Linky",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Report availability only when there is a usable reading."""
        return super().available and self.native_value is not None

    async def async_added_to_hass(self) -> None:
        """Restore the last reading until the coordinator has a result."""
        await super().async_added_to_hass()
        if not self._collection_enabled or not self.coordinator.last_update_success:
            return
        if self.coordinator.data is not None:
            self._set_latest_reading()
            return
        stored = await self.async_get_last_sensor_data()
        state = await self.async_get_last_state()
        if (
            stored is not None
            and stored.native_unit_of_measurement == self.native_unit_of_measurement
            and isinstance(stored.native_value, (int, float))
            and state is not None
            and state.state != STATE_UNAVAILABLE
        ):
            self._attr_native_value = stored.native_value
            self._attr_extra_state_attributes = {
                key: state.attributes[key]
                for key in (ATTR_LAST_DATE, ATTR_LAST_VALUE, ATTR_QUALITY, ATTR_USAGE_POINT_ID)
                if key in state.attributes
            }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Replace the restored reading with the result of an attempted refresh."""
        if self.coordinator.data is not None or not self.coordinator.last_update_success:
            self._set_latest_reading()
        super()._handle_coordinator_update()

    def _set_latest_reading(self) -> None:
        self._attr_native_value = None
        self._attr_extra_state_attributes = None
        if not self._collection_enabled or not self.coordinator.last_update_success:
            return
        data = getattr(self.coordinator.data, self.entity_description.data_key, None)
        if data and (
            reading := max(
                data.interval_reading,
                key=lambda reading: _reading_time(reading).timestamp(),
                default=None,
            )
        ):
            reading_date = reading.date
            if isinstance(reading_date, datetime):
                reading_date = _reading_time(reading).astimezone(PARIS_TZ)
            self._attr_native_value = reading.value
            self._attr_extra_state_attributes = {
                ATTR_LAST_DATE: reading_date.isoformat(),
                ATTR_LAST_VALUE: reading.value,
                ATTR_QUALITY: data.quality,
                ATTR_USAGE_POINT_ID: data.usage_point_id,
            }
