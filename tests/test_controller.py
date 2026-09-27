from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.select import ATTR_OPTION, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, async_mock_service

from custom_components.room_thermostat.const import (
    CONF_DEFAULT_ROOM,
    CONF_MANUAL_CHANGE_POLICY,
    CONF_MINIMUM_RANGE,
    CONF_SCHEDULE_ENTITY,
    EVENT_MANUAL_CHANGE,
)
from custom_components.room_thermostat.controller import build_rooms

from .conftest import (
    BED,
    LIVING,
    OFFICE,
    ROOM_CLIMATE,
    THERMOSTAT,
    make_entry,
    set_room,
    set_rooms,
    set_thermostat,
    setup_entry,
)

SCHEDULE = "schedule.rooms"
STATE = "sensor.house_room_control_state"
PROBLEM = "binary_sensor.house_room_control_problem"
ACTIVE_ROOM = "select.house_room_active_room"


def _writes(calls: AsyncMock) -> list[dict[str, Any]]:
    return [call.args[2] for call in calls.call_args_list if call.args[1] == SERVICE_SET_TEMPERATURE]


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    return await setup_entry(hass, make_entry(**options))


async def test_room_thermostat_shows_the_reference_room_and_the_thermostats_range(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    state = hass.states.get(ROOM_CLIMATE)
    assert state.state == "heat_cool"
    assert state.attributes["current_temperature"] == 66.6
    assert (state.attributes[ATTR_TARGET_TEMP_LOW], state.attributes[ATTR_TARGET_TEMP_HIGH]) == (55.0, 68.5)
    assert state.attributes["active_rooms"] == ["Living"]
    assert hass.states.get(ACTIVE_ROOM).state == "Living"
    assert hass.states.get(STATE).state == "controlling"
    assert hass.states.get(PROBLEM).state == STATE_OFF


async def test_first_evaluation_shifts_both_setpoints_by_the_offset(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    assert _writes(thermostat_calls) == [
        {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TARGET_TEMP_LOW: 52.5, ATTR_TARGET_TEMP_HIGH: 66.0}
    ]
    assert hass.states.get("sensor.house_room_room_offset").state == "-2.7"
    assert hass.states.get("sensor.house_room_commanded_heat_setpoint").state == "52.5"
    assert hass.states.get("sensor.house_room_commanded_cool_setpoint").state == "66.0"


async def test_routine_corrections_wait_for_the_minimum_interval(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    await _tick(hass, freezer, 14)
    set_rooms(hass)
    await _tick(hass, freezer, 0.5)
    before = len(_writes(thermostat_calls))
    await _tick(hass, freezer, 1)

    # THEN
    assert before == 1
    assert len(_writes(thermostat_calls)) == 2


async def test_no_write_when_the_thermostat_already_matches(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass, low=52.5, high=66.0, current=63.9)
    set_room(hass, LIVING, 63.9)
    set_room(hass, OFFICE, 60.0)
    set_room(hass, BED, 60.0)

    # WHEN
    await _setup(hass)

    # THEN
    assert _writes(thermostat_calls) == []


async def test_setting_a_room_range_writes_right_away(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)
    await _tick(hass, freezer, 1)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TARGET_TEMP_LOW: 62.0, ATTR_TARGET_TEMP_HIGH: 68.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    # THEN
    assert _writes(thermostat_calls)[-1] == {
        ATTR_ENTITY_ID: THERMOSTAT,
        ATTR_TARGET_TEMP_LOW: 59.5,
        ATTR_TARGET_TEMP_HIGH: 65.5,
    }
    state = hass.states.get(ROOM_CLIMATE)
    assert (state.attributes[ATTR_TARGET_TEMP_LOW], state.attributes[ATTR_TARGET_TEMP_HIGH]) == (62.0, 68.0)


async def test_a_room_range_narrower_than_the_minimum_is_rejected(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass, **{CONF_MINIMUM_RANGE: 5.0})

    # WHEN
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TARGET_TEMP_LOW: 66.0, ATTR_TARGET_TEMP_HIGH: 68.0},
            blocking=True,
        )

    # THEN
    assert err.value.translation_key == "range_too_narrow"


@pytest.mark.parametrize(
    ("mode", "expected_targets"),
    [("heat", (70.0, 75.0)), ("cool", (62.0, 70.0))],
)
async def test_a_single_target_keeps_the_minimum_range_to_the_other_side(
    hass: HomeAssistant, thermostat_calls: AsyncMock, mode: str, expected_targets: tuple[float, float]
) -> None:
    # GIVEN
    set_thermostat(hass, mode, temperature=68.0)
    set_rooms(hass)
    await _setup(hass, **{CONF_MINIMUM_RANGE: 5.0})

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TEMPERATURE: 70.0}, blocking=True
    )
    await hass.async_block_till_done()

    # THEN
    controller = hass.config_entries.async_entries("room_thermostat")[0].runtime_data
    assert (controller.targets.heat, controller.targets.cool) == expected_targets
    assert _writes(thermostat_calls)[-1] == {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 67.5}


async def test_a_single_target_for_a_mode_without_one_is_rejected_before_switching(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=66.0)
    set_rooms(hass)
    await _setup(hass)
    thermostat_calls.reset_mock()

    # WHEN
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_HVAC_MODE: "fan_only", ATTR_TEMPERATURE: 70.0},
            blocking=True,
        )

    # THEN
    assert err.value.translation_key == "single_target_not_applicable"
    assert thermostat_calls.call_args_list == []


async def test_mode_changes_pass_through_to_the_thermostat(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_HVAC_MODE: "cool"}, blocking=True
    )

    # THEN
    mode_calls = [call.args[2] for call in thermostat_calls.call_args_list if call.args[1] == SERVICE_SET_HVAC_MODE]
    assert mode_calls == [{ATTR_ENTITY_ID: THERMOSTAT, ATTR_HVAC_MODE: "cool"}]


async def test_set_temperature_with_a_mode_switches_first(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=66.0)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_HVAC_MODE: "cool", ATTR_TEMPERATURE: 74.0},
        blocking=True,
    )

    # THEN
    services = [call.args[1] for call in thermostat_calls.call_args_list]
    assert SERVICE_SET_HVAC_MODE in services
    assert hass.config_entries.async_entries("room_thermostat")[0].runtime_data.targets.cool == 74.0


async def test_heat_mode_writes_the_single_setpoint(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=66.0)
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    assert _writes(thermostat_calls) == [{ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 63.5}]
    assert hass.states.get(ROOM_CLIMATE).attributes[ATTR_TEMPERATURE] == 66.0


async def test_off_is_idle_and_never_writes(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass, "off")
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    assert _writes(thermostat_calls) == []
    assert hass.states.get(STATE).state == "idle"
    assert hass.states.get(ROOM_CLIMATE).attributes.get(ATTR_TEMPERATURE) is None


async def test_leaving_off_writes_right_away(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)
    set_thermostat(hass, "off")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 1)

    # WHEN
    set_thermostat(hass, "heat", temperature=55.0)
    await hass.async_block_till_done()

    # THEN
    assert _writes(thermostat_calls)[-1] == {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 52.5}


async def test_a_stale_active_room_falls_back_to_the_reference_room(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass, **{CONF_DEFAULT_ROOM: "office"})

    # WHEN
    await _tick(hass, freezer, 11)
    set_room(hass, LIVING, 66.6)
    await _tick(hass, freezer, 1)

    # THEN
    assert hass.states.get(STATE).state == "fallback_reference"
    problem = hass.states.get(PROBLEM)
    assert problem.state == STATE_ON
    assert problem.attributes["reasons"] == ["active_room_stale"]
    assert hass.states.get(ROOM_CLIMATE).attributes["current_temperature"] == 66.6


async def test_no_fresh_room_uses_the_reference_rooms_last_offset(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    await _tick(hass, freezer, 11)

    # THEN
    assert hass.states.get(STATE).state == "fallback_thermostat"
    assert hass.states.get(PROBLEM).attributes["reasons"] == ["no_fresh_room"]
    assert hass.states.get("sensor.house_room_room_offset").state == "-2.7"
    assert hass.states.get(ROOM_CLIMATE).attributes["current_temperature"] == 66.6


async def test_rooms_reporting_the_same_value_stay_fresh(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    for _ in range(3):
        await _tick(hass, freezer, 5)
        set_rooms(hass)
    await _tick(hass, freezer, 1)

    # THEN
    assert hass.states.get(STATE).state == "controlling"


async def test_a_celsius_room_sensor_is_converted(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    set_room(hass, LIVING, 20.0, unit="°C")

    # WHEN
    await _setup(hass)

    # THEN
    assert hass.states.get(ROOM_CLIMATE).attributes["current_temperature"] == 68.0


async def test_a_non_numeric_room_reading_is_ignored(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    set_room(hass, LIVING, STATE_UNAVAILABLE)

    # WHEN
    await _setup(hass)

    # THEN
    assert hass.states.get(STATE).state == "fallback_thermostat"


async def test_metric_installations_work_in_celsius(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    hass.config.units = METRIC_SYSTEM
    set_thermostat(hass, low=13.0, high=20.5, current=17.7, min_temp=10.0, max_temp=32.0)
    for entity_id, value in ((LIVING, 19.2), (OFFICE, 18.0), (BED, 19.0)):
        set_room(hass, entity_id, value, unit="°C")

    # WHEN
    await _setup(hass)

    # THEN
    assert _writes(thermostat_calls) == [
        {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TARGET_TEMP_LOW: 11.5, ATTR_TARGET_TEMP_HIGH: 19.0}
    ]


@pytest.mark.parametrize(
    ("option", "rooms"),
    [
        ("Office", ["Office"]),
        ("Average of all rooms", ["Bed", "Living", "Office"]),
        ("Room most off target", ["Office"]),
    ],
)
async def test_choosing_the_active_room(
    hass: HomeAssistant, thermostat_calls: AsyncMock, option: str, rooms: list[str]
) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=66.0)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: ACTIVE_ROOM, ATTR_OPTION: option}, blocking=True
    )
    await hass.async_block_till_done()

    # THEN
    assert hass.states.get(ACTIVE_ROOM).state == option
    assert hass.states.get(ROOM_CLIMATE).attributes["active_rooms"] == rooms
    assert len(_writes(thermostat_calls)) == 2


async def test_an_outside_change_holds_until_resumed(
    hass: HomeAssistant, thermostat_calls: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    # GIVEN
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_MANUAL_CHANGE, _record)
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    set_thermostat(hass, low=60.0, high=69.0)
    await hass.async_block_till_done()
    await _tick(hass, freezer, 30)
    held_writes = len(_writes(thermostat_calls))
    set_rooms(hass)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: "button.house_room_resume"}, blocking=True)
    await hass.async_block_till_done()

    # THEN
    assert held_writes == 1
    assert [event.data["policy"] for event in events] == ["hold"]
    assert events[0].data["heat"] == 60.0
    assert len(_writes(thermostat_calls)) == 2
    assert hass.states.get(STATE).state == "controlling"


async def test_an_outside_change_can_be_adopted(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    entry = await _setup(hass, **{CONF_MANUAL_CHANGE_POLICY: "adopt"})

    # WHEN
    set_thermostat(hass, low=60.0, high=69.0)
    await hass.async_block_till_done()

    # THEN
    targets = entry.runtime_data.targets
    assert (targets.heat, targets.cool) == pytest.approx((62.7, 71.7))
    assert hass.states.get(STATE).state == "controlling"


async def test_our_own_write_landing_is_not_an_outside_change(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass)

    # WHEN
    set_thermostat(hass, low=52.5, high=66.0)
    await hass.async_block_till_done()

    # THEN
    assert hass.states.get(STATE).state == "controlling"


async def test_a_failed_write_is_reported_as_a_problem(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    thermostat_calls.side_effect = HomeAssistantError("rejected")
    set_thermostat(hass)
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    assert hass.states.get(PROBLEM).attributes["reasons"] == ["write failed: rejected"]


async def test_an_unavailable_thermostat_is_a_problem(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    hass.states.async_set(THERMOSTAT, STATE_UNAVAILABLE)
    set_rooms(hass)

    # WHEN
    await _setup(hass)

    # THEN
    assert hass.states.get(STATE).state == "underlying_unavailable"
    assert hass.states.get(PROBLEM).attributes["reasons"] == ["thermostat_unavailable"]
    assert hass.states.get(ROOM_CLIMATE).attributes["hvac_modes"] == ["off"]
    assert _writes(thermostat_calls) == []


def _set_schedule(hass: HomeAssistant, state: str, **data: Any) -> None:
    hass.states.async_set(SCHEDULE, state, {"friendly_name": "Rooms", **data})


async def test_a_schedule_block_sets_room_and_targets_and_ends_a_hold(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    _set_schedule(hass, STATE_OFF)
    set_thermostat(hass)
    set_rooms(hass)
    entry = await _setup(hass, **{CONF_SCHEDULE_ENTITY: SCHEDULE})
    set_thermostat(hass, low=60.0, high=69.0)
    await hass.async_block_till_done()

    # WHEN
    _set_schedule(hass, STATE_ON, room="Bed", heat=64, cool=72)
    await hass.async_block_till_done()

    # THEN
    controller = entry.runtime_data
    assert hass.states.get(ACTIVE_ROOM).state == "Bed"
    assert (controller.targets.heat, controller.targets.cool) == (64.0, 72.0)
    assert hass.states.get(STATE).state == "controlling"
    assert _writes(thermostat_calls)[-1] == {
        ATTR_ENTITY_ID: THERMOSTAT,
        ATTR_TARGET_TEMP_LOW: 61.0,
        ATTR_TARGET_TEMP_HIGH: 69.0,
    }


async def test_leaving_a_schedule_block_returns_to_the_default_room(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    _set_schedule(hass, STATE_ON, room="office")
    set_thermostat(hass)
    set_rooms(hass)
    await _setup(hass, **{CONF_SCHEDULE_ENTITY: SCHEDULE})
    assert hass.states.get(ACTIVE_ROOM).state == "Office"

    # WHEN
    _set_schedule(hass, STATE_OFF)
    await hass.async_block_till_done()

    # THEN
    assert hass.states.get(ACTIVE_ROOM).state == "Living"


async def test_invalid_schedule_data_is_reported_and_the_rest_applied(
    hass: HomeAssistant, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    _set_schedule(hass, STATE_OFF)
    set_thermostat(hass)
    set_rooms(hass)
    entry = await _setup(hass, **{CONF_SCHEDULE_ENTITY: SCHEDULE, CONF_MINIMUM_RANGE: 5.0})

    # WHEN
    _set_schedule(hass, STATE_ON, room="garage", heat=70, cool=72)
    await hass.async_block_till_done()

    # THEN
    assert hass.states.get(PROBLEM).attributes["reasons"] == [
        "schedule: unknown room 'garage'",
        "schedule: heat and cool are closer than the minimum range",
    ]
    targets = entry.runtime_data.targets
    assert (targets.heat, targets.cool) == (70.0, 75.0)


async def test_state_survives_a_reload(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    entry = await _setup(hass)
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: ACTIVE_ROOM, ATTR_OPTION: "Bed"}, blocking=True
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TARGET_TEMP_LOW: 63.0, ATTR_TARGET_TEMP_HIGH: 70.0},
        blocking=True,
    )
    set_thermostat(hass, low=58.0, high=69.0)
    await hass.async_block_till_done()

    # WHEN
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # THEN
    controller = entry.runtime_data
    assert controller.selection.room_id == "bed"
    assert (controller.targets.heat, controller.targets.cool) == (63.0, 70.0)
    assert controller.hold is True
    assert controller.filters["bed"].value == pytest.approx(-3.1)


def test_room_ids_come_from_entity_ids_and_stay_unique() -> None:
    # WHEN
    rooms = build_rooms(["sensor.den_temperature", "sensor.den", "sensor.temperature"])

    # THEN
    assert rooms == {"den": "sensor.den_temperature", "den_2": "sensor.den", "temperature": "sensor.temperature"}


async def test_a_missing_room_sensor_counts_as_stale(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_room(hass, OFFICE, 65.0)
    set_room(hass, BED, 67.0)

    # WHEN
    await _setup(hass)

    # THEN
    assert hass.states.get(STATE).state == "fallback_thermostat"
    assert hass.states.get(ACTIVE_ROOM).attributes["options"][0] == "Living"


async def test_corrupt_stored_state_is_ignored(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    # GIVEN
    entry = make_entry()
    hass_storage[f"room_thermostat.{entry.entry_id}"] = {
        "version": 1,
        "key": f"room_thermostat.{entry.entry_id}",
        "data": {
            "selection": {"strategy": "bogus", "room_id": "nowhere"},
            "targets": {"heat": "warm", "cool": True},
            "filters": {"living": [None, "2026-01-01T00:00:00+00:00"], "gone": [1.0, "2026-01-01T00:00:00+00:00"]},
            "block_signature": "not-a-list",
        },
    }
    set_thermostat(hass)
    set_rooms(hass)

    # WHEN
    await setup_entry(hass, entry)

    # THEN
    controller = entry.runtime_data
    assert controller.selection.room_id == "living"
    assert (controller.targets.heat, controller.targets.cool) == (55.0, 68.5)
    assert set(controller.filters) == {"living", "office", "bed"}
    assert controller.block_signature is None


async def test_a_stored_strategy_is_restored(
    hass: HomeAssistant, thermostat_calls: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    # GIVEN
    entry = make_entry()
    hass_storage[f"room_thermostat.{entry.entry_id}"] = {
        "version": 1,
        "key": f"room_thermostat.{entry.entry_id}",
        "data": {"selection": {"strategy": "average", "room_id": None}, "targets": {"heat": 64, "cool": 72}},
    }
    set_thermostat(hass)
    set_rooms(hass)

    # WHEN
    await setup_entry(hass, entry)

    # THEN
    assert hass.states.get(ACTIVE_ROOM).state == "Average of all rooms"


@pytest.mark.parametrize(
    ("mode", "temperature", "expected"),
    [("heat", 64.0, (64.0, 70.0)), ("cool", 76.0, (70.0, 76.0)), ("off", None, (68.0, 74.0))],
)
async def test_first_targets_come_from_the_thermostat(
    hass: HomeAssistant,
    thermostat_calls: AsyncMock,
    mode: str,
    temperature: float | None,
    expected: tuple[float, float],
) -> None:
    # GIVEN
    set_thermostat(hass, mode, temperature=temperature)
    set_rooms(hass)

    # WHEN
    entry = await _setup(hass)

    # THEN
    targets = entry.runtime_data.targets
    assert (targets.heat, targets.cool) == expected


async def test_the_thermostat_is_called_through_its_climate_services(hass: HomeAssistant) -> None:
    # GIVEN
    set_thermostat(hass, "off")
    set_rooms(hass)
    entry = await _setup(hass)
    calls = async_mock_service(hass, CLIMATE_DOMAIN, "set_fan_mode")

    # WHEN
    await entry.runtime_data.async_call_thermostat("set_fan_mode", {ATTR_ENTITY_ID: THERMOSTAT, "fan_mode": "on"})

    # THEN
    assert [call.data for call in calls] == [{ATTR_ENTITY_ID: THERMOSTAT, "fan_mode": "on"}]
