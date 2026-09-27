from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.select import ATTR_OPTION, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.room_thermostat.const import CONF_DEFAULT_ROOM, CONF_UNSERVED_ROOMS
from custom_components.room_thermostat.control import Selection, Strategy
from custom_components.room_thermostat.planner import PlanError

from .conftest import BED, ROOM_CLIMATE, make_entry, set_rooms, set_thermostat, setup_entry

ACTIVE_ROOM = "select.house_room_active_room"


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    set_thermostat(hass, "heat", temperature=68.0)
    set_rooms(hass, living=66.0, office=65.0, bed=60.0)
    return await setup_entry(hass, make_entry(**{CONF_UNSERVED_ROOMS: [BED], **options}))


async def test_an_unserved_room_is_never_offered_to_follow(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # WHEN
    entry = await _setup(hass)

    # THEN
    assert hass.states.get(ACTIVE_ROOM).attributes["options"] == [
        "Living",
        "Office",
        "Average of all rooms",
        "Room most off target",
    ]
    assert list(entry.runtime_data.followable_rooms) == ["living", "office"]


async def test_average_and_most_off_target_leave_out_unserved_rooms(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    entry = await _setup(hass)
    controller = entry.runtime_data

    # WHEN
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: ACTIVE_ROOM, ATTR_OPTION: "Room most off target"},
        blocking=True,
    )

    # THEN
    assert controller.snapshot.room_ids == ("office",), "the colder bedroom is not the system's to heat"
    await controller.async_set_selection(Selection(Strategy.AVERAGE))
    assert controller.snapshot.room_ids == ("living", "office")
    assert hass.states.get(ROOM_CLIMATE).attributes["current_temperature"] == 65.5


async def test_presets_and_holds_cannot_follow_an_unserved_room(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    presets = [
        {"id": "home", "name": "Home", "heat": 68, "cool": 74, "room": "living"},
        {"id": "away", "name": "Away", "heat": 62, "cool": 80, "room": "average"},
        {"id": "sleep", "name": "Sleep", "heat": 64, "cool": 72, "room": "bed"},
    ]

    # WHEN
    await client.send_json_auto_id({"type": "room_thermostat/set", "entity_id": ROOM_CLIMATE, "room": "Bed"})
    held = await client.receive_json()
    await client.send_json_auto_id({"type": "room_thermostat/config", "entity_id": ROOM_CLIMATE})
    config = await client.receive_json()

    # THEN
    assert held["error"]["code"] == "invalid_format"
    with pytest.raises(PlanError, match="unknown room"):
        entry.runtime_data.async_save_presets(presets)
    assert config["result"]["rooms"][2] == {"id": "bed", "name": "Bed", "followable": False}


async def test_a_stored_choice_of_a_room_that_became_unserved_falls_back(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=68.0)
    set_rooms(hass)
    entry = await setup_entry(hass, make_entry(**{CONF_DEFAULT_ROOM: "office"}))
    await entry.runtime_data.async_set_selection(Selection.room("bed"))
    await entry.runtime_data.async_flush()

    # WHEN
    hass.config_entries.async_update_entry(entry, options=dict(entry.options) | {CONF_UNSERVED_ROOMS: [BED]})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # THEN
    assert entry.runtime_data.selection == Selection.room("office")
