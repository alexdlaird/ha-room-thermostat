"""What the controller sees and decides, as sensors with history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory

from .control import ControlState
from .entity import RoomThermostatEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from . import RoomThermostatConfigEntry
    from .controller import RoomThermostatController, Snapshot

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RoomSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Snapshot], StateType | datetime]
    #: Differences (offset, error) carry the unit but must not be converted like absolute temperatures.
    difference: bool = False


SENSORS: Final[tuple[RoomSensorDescription, ...]] = (
    RoomSensorDescription(
        key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda snapshot: snapshot.room_temperature,
    ),
    RoomSensorDescription(
        key="room_offset",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        difference=True,
        value_fn=lambda snapshot: snapshot.offset,
    ),
    RoomSensorDescription(
        key="room_error",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        difference=True,
        value_fn=lambda snapshot: None if snapshot.error is None else round(snapshot.error, 2),
    ),
    RoomSensorDescription(
        key="commanded_heat",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.commanded.heat,
    ),
    RoomSensorDescription(
        key="commanded_cool",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.commanded.cool,
    ),
    RoomSensorDescription(
        key="hold_ends",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda snapshot: snapshot.hold_until,
    ),
    RoomSensorDescription(
        key="control_state",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in ControlState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.state.value,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomThermostatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(RoomSensor(entry.runtime_data, description) for description in SENSORS)


class RoomSensor(RoomThermostatEntity, SensorEntity):
    entity_description: RoomSensorDescription

    def __init__(self, controller: RoomThermostatController, description: RoomSensorDescription) -> None:
        super().__init__(controller, description.key)
        self.entity_description = description
        if description.device_class is SensorDeviceClass.TEMPERATURE or description.difference:
            self._attr_native_unit_of_measurement = controller.unit

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.controller.snapshot)
