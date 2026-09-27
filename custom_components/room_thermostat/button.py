"""Resume: end a manual hold and control to the current targets again."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity

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
    async_add_entities([ResumeButton(entry.runtime_data, "resume")])


class ResumeButton(RoomThermostatEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.controller.async_resume()
