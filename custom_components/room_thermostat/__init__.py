"""Room Thermostat: control any thermostat to the temperature of the room that matters."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .controller import RoomThermostatController

type RoomThermostatConfigEntry = ConfigEntry[RoomThermostatController]

__all__ = ["RoomThermostatConfigEntry", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: RoomThermostatConfigEntry) -> bool:
    """Start the controller, then its entities."""
    controller = RoomThermostatController(hass, entry)
    await controller.async_start()
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoomThermostatConfigEntry) -> bool:
    """Unload the entities and save the controller's state; its subscriptions end with the entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_flush()
    return unloaded
