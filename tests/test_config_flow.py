from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.room_thermostat.const import (
    CONF_CLIMATE_ENTITY,
    CONF_DEADBAND,
    CONF_DEFAULT_ROOM,
    CONF_HOLD_DURATION,
    CONF_MANUAL_CHANGE_POLICY,
    CONF_MAX_OFFSET,
    CONF_MIN_WRITE_INTERVAL,
    CONF_MINIMUM_RANGE,
    CONF_OUTDOOR_SENSOR,
    CONF_REFERENCE_SENSOR,
    CONF_ROOM_SENSORS,
    CONF_SCHEDULE_ENTITY,
    CONF_SETPOINT_STEP,
    CONF_SMOOTHING,
    CONF_STALE_AFTER,
    CONF_UNSERVED_ROOMS,
    DOMAIN,
)

from .conftest import BED, LIVING, OFFICE, THERMOSTAT, make_entry, set_rooms, set_thermostat, setup_entry

USER_INPUT = {
    CONF_NAME: "House room",
    CONF_CLIMATE_ENTITY: THERMOSTAT,
    CONF_ROOM_SENSORS: [LIVING, OFFICE],
    CONF_REFERENCE_SENSOR: LIVING,
}


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_user_flow_creates_an_entry_with_unit_defaults(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    result = await _start(hass)

    # WHEN
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    # THEN
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "House room"
    assert result["data"] == {CONF_CLIMATE_ENTITY: THERMOSTAT}
    assert result["options"] == {
        CONF_ROOM_SENSORS: [LIVING, OFFICE],
        CONF_REFERENCE_SENSOR: LIVING,
        CONF_STALE_AFTER: 10,
        CONF_MAX_OFFSET: 6.0,
        CONF_DEADBAND: 0.5,
        CONF_MIN_WRITE_INTERVAL: 15,
        CONF_SMOOTHING: 15,
        CONF_SETPOINT_STEP: 0.5,
        CONF_MINIMUM_RANGE: 0.0,
        CONF_MANUAL_CHANGE_POLICY: "hold",
        CONF_HOLD_DURATION: 120,
    }


async def test_the_reference_room_is_optional(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    result = await _start(hass)
    user_input = {key: value for key, value in USER_INPUT.items() if key != CONF_REFERENCE_SENSOR}

    # WHEN
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()

    # THEN
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_REFERENCE_SENSOR not in result["options"]


@pytest.mark.parametrize(
    ("override", "field", "error"),
    [
        ({CONF_ROOM_SENSORS: []}, CONF_ROOM_SENSORS, "no_rooms"),
        ({CONF_REFERENCE_SENSOR: BED}, CONF_REFERENCE_SENSOR, "reference_not_a_room"),
    ],
)
async def test_user_flow_validates_rooms(hass: HomeAssistant, override: dict[str, Any], field: str, error: str) -> None:
    # GIVEN
    result = await _start(hass)

    # WHEN
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT | override)

    # THEN
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}


async def test_a_room_thermostat_cannot_wrap_another(hass: HomeAssistant) -> None:
    # GIVEN
    er.async_get(hass).async_get_or_create("climate", DOMAIN, "other", suggested_object_id="wrapped")
    result = await _start(hass)

    # WHEN
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {CONF_CLIMATE_ENTITY: "climate.wrapped"}
    )

    # THEN
    assert result["errors"] == {CONF_CLIMATE_ENTITY: "cannot_wrap_itself"}


async def test_one_room_thermostat_per_thermostat(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    await setup_entry(hass, make_entry())
    result = await _start(hass)

    # WHEN
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    # THEN
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_are_saved_and_reload_the_entry(hass: HomeAssistant, thermostat_calls: AsyncMock) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    new_options = dict(entry.options) | {
        CONF_DEFAULT_ROOM: "office",
        CONF_SCHEDULE_ENTITY: "schedule.rooms",
        CONF_MANUAL_CHANGE_POLICY: "adopt",
        CONF_MINIMUM_RANGE: 5.0,
        CONF_OUTDOOR_SENSOR: "sensor.outdoor_temperature",
    }

    # WHEN
    result = await hass.config_entries.options.async_configure(result["flow_id"], new_options)
    await hass.async_block_till_done()

    # THEN
    assert result["type"] is FlowResultType.CREATE_ENTRY
    controller = entry.runtime_data
    assert controller.default_selection.room_id == "office"
    assert controller.schedule_entity_id == "schedule.rooms"
    assert controller.policy == "adopt"
    assert controller.settings.minimum_range == 5.0
    assert controller.outdoor_entity_id == "sensor.outdoor_temperature"


@pytest.mark.parametrize(
    ("override", "field", "error"),
    [
        ({CONF_DEFAULT_ROOM: "garage"}, CONF_DEFAULT_ROOM, "unknown_default_room"),
        ({CONF_ROOM_SENSORS: [OFFICE, BED]}, CONF_REFERENCE_SENSOR, "reference_not_a_room"),
        ({CONF_UNSERVED_ROOMS: ["sensor.attic_temperature"]}, CONF_UNSERVED_ROOMS, "unserved_not_a_room"),
        ({CONF_UNSERVED_ROOMS: [LIVING, OFFICE, BED]}, CONF_UNSERVED_ROOMS, "no_served_rooms"),
        ({CONF_UNSERVED_ROOMS: [LIVING]}, CONF_REFERENCE_SENSOR, "reference_unserved"),
        ({CONF_UNSERVED_ROOMS: [OFFICE], CONF_DEFAULT_ROOM: "office"}, CONF_DEFAULT_ROOM, "unknown_default_room"),
    ],
)
async def test_options_are_validated(
    hass: HomeAssistant, thermostat_calls: AsyncMock, override: dict[str, Any], field: str, error: str
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # WHEN
    result = await hass.config_entries.options.async_configure(result["flow_id"], dict(entry.options) | override)

    # THEN
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][field] == error


@pytest.mark.parametrize("default_room", ["average", "extreme", "bed"])
async def test_options_accept_strategies_and_room_ids_as_the_default(
    hass: HomeAssistant, thermostat_calls: AsyncMock, default_room: str
) -> None:
    # GIVEN
    set_thermostat(hass)
    set_rooms(hass)
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # WHEN
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(entry.options) | {CONF_DEFAULT_ROOM: default_room}
    )

    # THEN
    assert result["type"] is FlowResultType.CREATE_ENTRY
