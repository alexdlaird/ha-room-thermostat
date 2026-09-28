from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import (
    ATTR_FAN_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.room_thermostat.const import CONF_SCHEDULE_ENTITY, EVENT_OVERRIDE_ENDED
from custom_components.room_thermostat.planner import Hold, HoldKind, parse_hold

from .conftest import BED, LIVING, OFFICE, ROOM_CLIMATE, THERMOSTAT, make_entry, set_rooms, set_thermostat, setup_entry

SCHEDULE_HELPER = "schedule.rooms"


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    """Local wall-clock time in the week of Monday 2026-09-28 (day 0 = Monday)."""
    zone = dt_util.get_default_time_zone()
    return datetime(2026, 9, 28 + day, hour, minute, tzinfo=zone)


def _week(**days: list[tuple[str, str]]) -> list[list[dict[str, str]]]:
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return [[{"time": at, "preset": preset} for at, preset in days.get(name, [])] for name in names]


async def _move_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    set_thermostat(hass)
    set_rooms(hass)
    return await setup_entry(hass, make_entry(**options))


async def _ws(client: Any, command: str, **data: Any) -> dict[str, Any]:
    """One command on an already authenticated client (its token would expire as the frozen clock moves)."""
    await client.send_json_auto_id({"type": f"room_thermostat/{command}", "entity_id": ROOM_CLIMATE, **data})
    response: dict[str, Any] = await client.receive_json()
    return response


def _minutes(minutes: int) -> Hold:
    return parse_hold({"kind": "minutes", "minutes": minutes}, dt_util.utcnow())


def _targets(entry: MockConfigEntry) -> tuple[float | None, float | None]:
    return entry.runtime_data.targets.heat, entry.runtime_data.targets.cool


def _presets_with(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": "home", "name": "Home", "heat": 68, "cool": 74, "room": "living"},
        {"id": "away", "name": "Away", "heat": 62, "cool": 80, "room": "average"},
        {"id": "sleep", "name": "Sleep", "heat": 64, "cool": 72, "room": "bed"},
        *extra,
    ]


async def test_a_new_room_thermostat_offers_home_away_and_sleep(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # WHEN
    await _setup(hass)

    # THEN
    state = hass.states.get(ROOM_CLIMATE)
    assert state.attributes["preset_modes"] == ["Home", "Away", "Sleep"]
    assert state.attributes[ATTR_PRESET_MODE] is None
    assert state.attributes["config_revision"] == 0


async def test_choosing_a_preset_sets_its_room_and_targets(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    entry = await _setup(hass)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_PRESET_MODE: "Away"}, blocking=True
    )

    # THEN
    assert _targets(entry) == (62.0, 80.0)
    assert entry.runtime_data.selection.strategy == "average"
    state = hass.states.get(ROOM_CLIMATE)
    assert state.attributes[ATTR_PRESET_MODE] == "Away"
    assert state.attributes["preset_id"] == "away"


async def test_an_unknown_preset_is_rejected(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    entry = await _setup(hass)

    # THEN
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_activate_preset("party")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_PRESET_MODE: "Party"},
            blocking=True,
        )


async def test_saving_a_schedule_applies_the_block_in_effect(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)

    # WHEN
    entry.runtime_data.async_save_schedule(_week(mon=[("07:00", "sleep"), ("22:00", "away")]))
    await hass.async_block_till_done()

    # THEN
    assert entry.runtime_data.active_preset == "sleep"
    assert entry.runtime_data.config_revision == 1
    assert hass.states.get(ROOM_CLIMATE).attributes["schedule_next_change"] == _local(0, 22).isoformat()
    assert hass.states.get("sensor.house_room_schedule").state == "in_block"


async def test_the_schedule_is_saved_over_websocket(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    await _setup(hass)
    client = await hass_ws_client(hass)

    # WHEN
    response = await _ws(client, "schedule/save", schedule=_week(mon=[("07:00", "sleep"), ("22:00", "away")]))

    # THEN
    assert response["success"], response
    assert response["result"]["schedule"][0] == [
        {"time": "07:00", "preset": "sleep"},
        {"time": "22:00", "preset": "away"},
    ]
    assert response["result"]["revision"] == 1


async def test_each_block_applies_its_preset_and_ends_a_next_block_hold(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)
    controller = entry.runtime_data
    controller.async_save_schedule(_week(mon=[("07:00", "home"), ("22:00", "sleep")]))
    ended: list[Event] = []

    @callback
    def record(event: Event) -> None:
        ended.append(event)

    hass.bus.async_listen(EVENT_OVERRIDE_ENDED, record)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TARGET_TEMP_LOW: 70.0, ATTR_TARGET_TEMP_HIGH: 75.0},
        blocking=True,
    )
    assert controller.override is not None
    assert controller.active_preset is None

    # WHEN
    await _move_to(hass, freezer, _local(0, 22, 1))

    # THEN
    assert controller.active_preset == "sleep"
    assert controller.override is None
    assert [event.data["reason"] for event in ended] == ["schedule"]


async def test_a_timed_hold_outlasts_a_block_then_returns_to_the_schedule(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 21))
    entry = await _setup(hass)
    controller = entry.runtime_data
    controller.async_save_schedule(_week(mon=[("07:00", "home"), ("22:00", "sleep")]))

    # WHEN
    await controller.async_set_targets(heat=71, cool=76, hold=_minutes(120))
    await _move_to(hass, freezer, _local(0, 22, 30))

    # THEN
    assert _targets(entry) == (71.0, 76.0), "the hold outranks the 22:00 block"
    await _move_to(hass, freezer, _local(0, 23, 1))
    assert controller.active_preset == "sleep"
    assert controller.override is None


async def test_an_indefinite_hold_ignores_blocks_until_resumed(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)
    controller = entry.runtime_data
    controller.async_save_schedule(_week(mon=[("07:00", "home"), ("22:00", "sleep")]))
    await controller.async_activate_preset("away", hold=Hold(HoldKind.INDEFINITE))

    # WHEN
    await _move_to(hass, freezer, _local(1, 1))

    # THEN
    assert controller.active_preset == "away"
    await controller.async_resume()
    assert controller.active_preset == "sleep"
    assert controller.override is None


async def test_holds_and_resume_over_websocket(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    entry.runtime_data.async_save_schedule(
        _week(**{day: [("00:00", "home")] for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    )

    # WHEN
    held = await _ws(client, "set", preset="away", hold={"kind": "indefinite"})
    resumed = await _ws(client, "resume")

    # THEN
    assert held["result"]["hold"] == {"kind": "indefinite", "until": None}
    assert held["result"]["active_preset"] == "away"
    assert resumed["result"]["hold"] is None
    assert resumed["result"]["active_preset"] == "home"


async def test_without_a_schedule_a_timed_hold_returns_to_what_ran_before(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)
    controller = entry.runtime_data
    await controller.async_set_targets(heat=67, cool=73)
    assert controller.override is None, "with no schedule a new target is simply the new setting"
    before = _targets(entry)

    # WHEN
    await controller.async_activate_preset("away", hold=_minutes(60))
    await _move_to(hass, freezer, _local(0, 9, 1))

    # THEN
    assert _targets(entry) == before
    assert controller.active_preset is None
    assert controller.override is None


async def test_without_a_schedule_a_preset_is_a_toggle_over_the_normal_setting(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    entry = await _setup(hass)
    controller = entry.runtime_data
    await controller.async_set_targets(heat=67, cool=73)

    # WHEN
    await controller.async_activate_preset("away")

    # THEN
    assert controller.override == Hold(HoldKind.INDEFINITE)
    assert _targets(entry) == (62.0, 80.0)
    await controller.async_resume()
    assert _targets(entry) == (67.0, 73.0)
    assert controller.active_preset is None
    assert controller.override is None


async def test_moving_the_dial_during_a_preset_makes_a_new_normal(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    entry = await _setup(hass)
    controller = entry.runtime_data
    await controller.async_activate_preset("away")

    # WHEN
    await controller.async_set_targets(heat=69, cool=75)

    # THEN
    assert controller.override is None
    assert controller.baseline is None
    assert controller.active_preset is None


async def test_editing_the_active_preset_applies_it_and_new_presets_get_ids(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    await entry.runtime_data.async_activate_preset("home")

    # WHEN
    response = await _ws(
        client,
        "presets/save",
        presets=[
            {"id": "home", "name": "Home", "heat": 69, "cool": 75, "room": "Office"},
            *_presets_with()[1:],
            {"name": "Movie night", "heat": 70, "cool": 73, "room": "living"},
        ],
    )

    # THEN
    assert response["success"], response
    assert [preset["id"] for preset in response["result"]["presets"]] == ["home", "away", "sleep", "movie_night"]
    assert _targets(entry) == (69.0, 75.0)
    assert entry.runtime_data.selection.room_id == "office"
    assert hass.states.get(ROOM_CLIMATE).attributes["preset_modes"][-1] == "Movie night"


async def test_a_preset_the_schedule_uses_cannot_be_deleted(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    entry.runtime_data.async_save_presets(_presets_with({"name": "Movie", "heat": 70, "cool": 73, "room": "living"}))
    entry.runtime_data.async_save_schedule(_week(fri=[("19:00", "movie")]))

    # WHEN
    response = await _ws(client, "presets/save", presets=_presets_with())

    # THEN
    assert not response["success"]
    assert "Movie" in response["error"]["message"]


async def test_deleting_the_active_preset_leaves_no_active_preset(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    entry = await _setup(hass)
    controller = entry.runtime_data
    controller.async_save_presets(_presets_with({"name": "Movie", "heat": 70, "cool": 73, "room": "living"}))
    await controller.async_activate_preset("movie")

    # WHEN
    controller.async_save_presets(_presets_with())

    # THEN
    assert controller.active_preset is None
    assert _targets(entry) == (70.0, 73.0)


async def test_bad_requests_get_clear_errors(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    await _setup(hass)
    client = await hass_ws_client(hass)

    # WHEN
    await client.send_json_auto_id({"type": "room_thermostat/config", "entity_id": THERMOSTAT})
    not_ours = await client.receive_json()
    bad_room = await _ws(client, "set", room="attic")
    bad_hold = await _ws(client, "set", heat=70, hold={"kind": "forever"})
    narrow = await _ws(client, "set", preset="party")
    bad_schedule = await _ws(client, "schedule/save", schedule=[[]])

    # THEN
    assert not_ours["error"]["code"] == "not_found"
    for response in (bad_room, bad_hold, narrow, bad_schedule):
        assert response["error"]["code"] == "invalid_format", response
    assert "attic" in bad_room["error"]["message"]
    assert narrow["error"]["message"]


async def test_config_describes_rooms_presets_and_the_week(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_ws_client: WebSocketGenerator
) -> None:
    # GIVEN
    await _setup(hass)
    client = await hass_ws_client(hass)
    await _ws(client, "set", room="Bed")

    # WHEN
    response = await _ws(client, "config")

    # THEN
    result = response["result"]
    assert result["unit"] == "°F"
    assert result["rooms"] == [
        {"id": "living", "name": "Living", "entity_id": LIVING, "followable": True},
        {"id": "office", "name": "Office", "entity_id": OFFICE, "followable": True},
        {"id": "bed", "name": "Bed", "entity_id": BED, "followable": True},
    ]
    assert result["history"] == {
        "room_temperature": "sensor.house_room_room_temperature",
        "thermostat_temperature": "sensor.house_room_thermostat_temperature",
        "commanded_heat": "sensor.house_room_commanded_heat_setpoint",
        "commanded_cool": "sensor.house_room_commanded_cool_setpoint",
        "outdoor_temperature": None,
        "thermostat": THERMOSTAT,
    }
    assert hass.states.get("sensor.house_room_thermostat_temperature").state == "63.9"
    assert [preset["name"] for preset in result["presets"]] == ["Home", "Away", "Sleep"]
    assert result["schedule"] == [[]] * 7
    assert result["selection"] == "bed"
    assert result["hold"] is None


async def test_presets_schedule_and_holds_survive_a_reload(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)
    controller = entry.runtime_data
    controller.async_save_presets(_presets_with({"name": "Movie", "heat": 70, "cool": 73, "room": "living"}))
    controller.async_save_schedule(_week(mon=[("07:00", "home")]))
    await controller.async_activate_preset("movie", hold=_minutes(90))

    # WHEN
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # THEN
    controller = entry.runtime_data
    assert list(controller.presets) == ["home", "away", "sleep", "movie"]
    assert controller.schedule[0][0].preset_id == "home"
    assert controller.active_preset == "movie"
    assert controller.override is not None
    assert controller.baseline is not None
    assert controller.config_revision == 2


async def test_an_unreadable_stored_baseline_is_ignored(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    entry = await _setup(hass)
    controller = entry.runtime_data

    # THEN
    assert controller._restore_baseline("junk") is None
    assert controller._restore_baseline({"targets": {"heat": 60}}) is None
    assert controller._restore_baseline(
        {"targets": {"heat": 60, "cool": 70}, "selection": "attic", "preset": "gone"}
    ) == (
        controller.targets.__class__(60.0, 70.0),
        controller.default_selection,
        None,
    )


async def test_a_schedule_helper_block_ends_a_hold(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    hass.states.async_set(SCHEDULE_HELPER, STATE_OFF)
    entry = await _setup(hass, **{CONF_SCHEDULE_ENTITY: SCHEDULE_HELPER})
    controller = entry.runtime_data
    await controller.async_activate_preset("away")
    assert controller.override is not None

    # WHEN
    hass.states.async_set(SCHEDULE_HELPER, STATE_ON, {"heat": 66, "cool": 72})
    await hass.async_block_till_done()

    # THEN
    assert controller.override is None
    assert _targets(entry) == (66.0, 72.0)


async def test_fan_modes_pass_through_to_the_thermostat(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_rooms(hass)
    set_thermostat(hass, fan_modes=["auto", "on", "circulate"], fan_mode="auto")
    await setup_entry(hass, make_entry())

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE, {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_FAN_MODE: "circulate"}, blocking=True
    )

    # THEN
    state = hass.states.get(ROOM_CLIMATE)
    assert state.attributes["fan_modes"] == ["auto", "on", "circulate"]
    assert state.attributes[ATTR_FAN_MODE] == "auto"
    fan_calls = [call.args[2] for call in thermostat_calls.call_args_list if call.args[1] == SERVICE_SET_FAN_MODE]
    assert fan_calls == [{ATTR_ENTITY_ID: THERMOSTAT, ATTR_FAN_MODE: "circulate"}]


async def test_a_thermostat_without_fan_modes_offers_none(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # WHEN
    await _setup(hass)

    # THEN
    assert "fan_modes" not in hass.states.get(ROOM_CLIMATE).attributes
    assert hass.states.get(ROOM_CLIMATE).attributes.get("override") is None


async def test_a_timed_hold_shows_on_the_climate_entity(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    freezer.move_to(_local(0, 8))
    entry = await _setup(hass)

    # WHEN
    await entry.runtime_data.async_set_targets(heat=70, cool=75, hold=_minutes(30))
    await hass.async_block_till_done()

    # THEN
    override = hass.states.get(ROOM_CLIMATE).attributes["override"]
    assert override["kind"] == "until"
    assert dt_util.parse_datetime(override["until"]) == dt_util.utcnow() + timedelta(minutes=30)
