"""WebSocket commands for apps: read and edit presets and the schedule, set holds, resume.

Any signed-in Home Assistant user may call these (unlike schedule helpers, whose editing is admin
only), so everyone in the household can manage the thermostat from an app. Each command names the
room thermostat by its climate entity id.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.const import ERR_INVALID_FORMAT, ERR_NOT_FOUND
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN
from .control import Selection, parse_selection
from .controller import RoomThermostatController
from .planner import PlanError, parse_hold

ENTITY: dict[Any, Any] = {vol.Required("entity_id"): cv.entity_id}


@callback
def async_register(hass: HomeAssistant) -> None:
    for command in (ws_config, ws_save_presets, ws_save_schedule, ws_set, ws_resume):
        async_register_command(hass, command)


def _controller(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> RoomThermostatController | None:
    entry_id = None
    if (registry_entry := er.async_get(hass).async_get(msg["entity_id"])) is not None:
        entry_id = registry_entry.config_entry_id
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_FOUND, f"{msg['entity_id']} is not a room thermostat")
        return None
    controller: RoomThermostatController = entry.runtime_data
    return controller


def _config_result(connection: ActiveConnection, msg: dict[str, Any], controller: RoomThermostatController) -> None:
    connection.send_result(msg["id"], controller.config())


@websocket_command({vol.Required("type"): f"{DOMAIN}/config", **ENTITY})
@callback
def ws_config(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Presets, the schedule, rooms, the active preset and hold."""
    if controller := _controller(hass, connection, msg):
        _config_result(connection, msg, controller)


@websocket_command({vol.Required("type"): f"{DOMAIN}/presets/save", **ENTITY, vol.Required("presets"): list})
@callback
def ws_save_presets(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Replace the preset list: [{id?, name, heat, cool, room}]; omit `id` for a new preset."""
    if controller := _controller(hass, connection, msg):
        try:
            controller.async_save_presets(msg["presets"])
        except PlanError as err:
            connection.send_error(msg["id"], ERR_INVALID_FORMAT, str(err))
            return
        _config_result(connection, msg, controller)


@websocket_command({vol.Required("type"): f"{DOMAIN}/schedule/save", **ENTITY, vol.Required("schedule"): list})
@callback
def ws_save_schedule(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Replace the week: seven day lists (Monday first) of {time: "HH:MM", preset: id}."""
    if controller := _controller(hass, connection, msg):
        try:
            controller.async_save_schedule(msg["schedule"])
        except PlanError as err:
            connection.send_error(msg["id"], ERR_INVALID_FORMAT, str(err))
            return
        _config_result(connection, msg, controller)


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set",
        **ENTITY,
        vol.Optional("preset"): str,
        vol.Optional("heat"): vol.Coerce(float),
        vol.Optional("cool"): vol.Coerce(float),
        vol.Optional("room"): str,
        vol.Optional("hold"): dict,
    }
)
@async_response
async def ws_set(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """A preset, room targets and/or the room to follow, held as asked (default: until the next block)."""
    controller = _controller(hass, connection, msg)
    if controller is None:
        return
    try:
        hold = parse_hold(msg.get("hold"), dt_util.utcnow())
        selection = _selection(controller, msg["room"]) if "room" in msg else None
        if "preset" in msg:
            await controller.async_activate_preset(msg["preset"], hold)
        if "heat" in msg or "cool" in msg:
            await controller.async_set_targets(heat=msg.get("heat"), cool=msg.get("cool"), hold=hold)
        if selection is not None:
            await controller.async_set_selection(selection, hold)
    except (PlanError, ServiceValidationError) as err:
        connection.send_error(msg["id"], ERR_INVALID_FORMAT, _message(err))
        return
    _config_result(connection, msg, controller)


def _selection(controller: RoomThermostatController, room: str) -> Selection:
    selection = parse_selection(room, controller.followable_rooms)
    if selection is None:
        raise PlanError(f"unknown room {room!r}")
    return selection


def _message(err: Exception) -> str:
    if isinstance(err, ServiceValidationError):
        return str(err) or err.translation_key or type(err).__name__
    return str(err)


@websocket_command({vol.Required("type"): f"{DOMAIN}/resume", **ENTITY})
@async_response
async def ws_resume(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """End any hold and return to the schedule (or what ran before the hold)."""
    if controller := _controller(hass, connection, msg):
        await controller.async_resume()
        _config_result(connection, msg, controller)
