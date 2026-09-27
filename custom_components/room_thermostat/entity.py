"""Base entity: one device per room thermostat, refreshed whenever the controller re-evaluates."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import RoomThermostatController


class RoomThermostatEntity(Entity):
    """Shared device, naming and update wiring."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: RoomThermostatController, key: str | None) -> None:
        self.controller = controller
        entry = controller.entry
        self._attr_unique_id = entry.entry_id if key is None else f"{entry.entry_id}-{key}"
        if key is not None:
            self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.controller.async_add_listener(self.async_write_ha_state))
