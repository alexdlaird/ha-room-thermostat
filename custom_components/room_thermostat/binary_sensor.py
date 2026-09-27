"""On while the controller cannot do its job as configured; `reasons` says why."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import RoomThermostatEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import RoomThermostatConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomThermostatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ControlProblemSensor(entry.runtime_data, "control_problem")])


class ControlProblemSensor(RoomThermostatEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return bool(self.controller.snapshot.problems)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reasons": list(self.controller.snapshot.problems)}
