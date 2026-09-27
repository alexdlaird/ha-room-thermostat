from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.room_thermostat.config_flow import default_options
from custom_components.room_thermostat.const import (
    CONF_CLIMATE_ENTITY,
    CONF_REFERENCE_SENSOR,
    CONF_ROOM_SENSORS,
    DOMAIN,
)
from custom_components.room_thermostat.controller import RoomThermostatController

THERMOSTAT = "climate.house"
LIVING = "sensor.living_temperature"
OFFICE = "sensor.office_temperature"
BED = "sensor.bed_temperature"
ROOM_CLIMATE = "climate.house_room"
MODES = ["off", "heat", "cool", "heat_cool", "fan_only"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture(autouse=True)
def imperial(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM


@pytest.fixture
def thermostat_calls() -> Generator[AsyncMock]:
    with patch.object(RoomThermostatController, "async_call_thermostat", autospec=True) as mock:
        yield mock


def set_thermostat(
    hass: HomeAssistant,
    mode: str = "heat_cool",
    *,
    current: float | None = 63.9,
    low: float = 55.0,
    high: float = 68.5,
    temperature: float | None = None,
    **extra: Any,
) -> None:
    attributes: dict[str, Any] = {
        "hvac_modes": MODES,
        "current_temperature": current,
        "min_temp": 50.0,
        "max_temp": 90.0,
        "hvac_action": "idle",
        "friendly_name": "House",
    }
    if mode == "heat_cool":
        attributes |= {"target_temp_low": low, "target_temp_high": high}
    elif mode in ("heat", "cool"):
        attributes["temperature"] = temperature
    hass.states.async_set(THERMOSTAT, mode, attributes | extra)


def set_room(hass: HomeAssistant, entity_id: str, value: float | str, unit: str = "°F") -> None:
    name = entity_id.split(".")[1].removesuffix("_temperature").title() + " Temperature"
    hass.states.async_set(
        entity_id, str(value), {"unit_of_measurement": unit, "device_class": "temperature", "friendly_name": name}
    )


def set_rooms(hass: HomeAssistant, living: float = 66.6, office: float = 65.0, bed: float = 67.0) -> None:
    set_room(hass, LIVING, living)
    set_room(hass, OFFICE, office)
    set_room(hass, BED, bed)


def make_entry(**options: Any) -> MockConfigEntry:
    base = {CONF_ROOM_SENSORS: [LIVING, OFFICE, BED], CONF_REFERENCE_SENSOR: LIVING}
    return MockConfigEntry(
        domain=DOMAIN,
        title="House room",
        unique_id=THERMOSTAT,
        data={CONF_CLIMATE_ENTITY: THERMOSTAT},
        options=base | options,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry, **defaults_override: Any) -> MockConfigEntry:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options=default_options(hass) | defaults_override | dict(entry.options)
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
