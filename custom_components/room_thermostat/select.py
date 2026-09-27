"""Active room: a configured room, the average of all rooms, or the room most off target."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.components.select import SelectEntity

from .control import Selection, Strategy
from .entity import RoomThermostatEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import RoomThermostatConfigEntry

PARALLEL_UPDATES = 0

OPTION_AVERAGE: Final = "Average of all rooms"
OPTION_EXTREME: Final = "Room most off target"
STRATEGY_OPTIONS: Final = {Strategy.AVERAGE: OPTION_AVERAGE, Strategy.EXTREME: OPTION_EXTREME}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomThermostatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ActiveRoomSelect(entry.runtime_data, "active_room")])


class ActiveRoomSelect(RoomThermostatEntity, SelectEntity):
    """Which room the thermostat holds; a choice here stands until the next schedule block."""

    @property
    def options(self) -> list[str]:
        return [*self.controller.room_names.values(), OPTION_AVERAGE, OPTION_EXTREME]

    @property
    def current_option(self) -> str | None:
        selection = self.controller.selection
        if selection.strategy is Strategy.ROOM:
            return self.controller.room_names.get(selection.room_id or "")
        return STRATEGY_OPTIONS[selection.strategy]

    async def async_select_option(self, option: str) -> None:
        for strategy, label in STRATEGY_OPTIONS.items():
            if option == label:
                await self.controller.async_set_selection(Selection(strategy))
                return
        room_id = next(room_id for room_id, name in self.controller.room_names.items() if name == option)
        await self.controller.async_set_selection(Selection.room(room_id))
