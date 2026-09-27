"""Set up a room thermostat in the UI; every tunable lives in the options (Configure)."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DEADBAND,
    CONF_DEFAULT_ROOM,
    CONF_HOLD_DURATION,
    CONF_MANUAL_CHANGE_POLICY,
    CONF_MAX_OFFSET,
    CONF_MIN_WRITE_INTERVAL,
    CONF_MINIMUM_RANGE,
    CONF_REFERENCE_SENSOR,
    CONF_ROOM_SENSORS,
    CONF_SCHEDULE_ENTITY,
    CONF_SETPOINT_STEP,
    CONF_SMOOTHING,
    CONF_STALE_AFTER,
    DEFAULT_HOLD_DURATION,
    DEFAULT_MIN_WRITE_INTERVAL,
    DEFAULT_SMOOTHING,
    DEFAULT_STALE_AFTER,
    DOMAIN,
)
from .control import ManualChangePolicy, Strategy
from .controller import build_rooms, unit_defaults

DEFAULT_NAME: Final = "Room thermostat"

ROOM_SENSOR_SELECTOR: Final = EntitySelector(
    EntitySelectorConfig(domain=SENSOR_DOMAIN, device_class=SensorDeviceClass.TEMPERATURE, multiple=True)
)
REFERENCE_SELECTOR: Final = EntitySelector(
    EntitySelectorConfig(domain=SENSOR_DOMAIN, device_class=SensorDeviceClass.TEMPERATURE)
)


def default_options(hass: HomeAssistant) -> dict[str, Any]:
    """Tunables for a new entry, scaled to the configured temperature unit."""
    max_offset, deadband, step = unit_defaults(hass.config.units.temperature_unit)
    return {
        CONF_STALE_AFTER: DEFAULT_STALE_AFTER,
        CONF_MAX_OFFSET: max_offset,
        CONF_DEADBAND: deadband,
        CONF_MIN_WRITE_INTERVAL: DEFAULT_MIN_WRITE_INTERVAL,
        CONF_SMOOTHING: DEFAULT_SMOOTHING,
        CONF_SETPOINT_STEP: step,
        CONF_MINIMUM_RANGE: 0.0,
        CONF_MANUAL_CHANGE_POLICY: ManualChangePolicy.HOLD.value,
        CONF_HOLD_DURATION: DEFAULT_HOLD_DURATION,
    }


def validate_rooms(user_input: dict[str, Any]) -> dict[str, str]:
    """At least one room, and the reference room must be one of them."""
    errors: dict[str, str] = {}
    rooms = user_input.get(CONF_ROOM_SENSORS) or []
    if not rooms:
        errors[CONF_ROOM_SENSORS] = "no_rooms"
    reference = user_input.get(CONF_REFERENCE_SENSOR)
    if reference and rooms and reference not in rooms:
        errors[CONF_REFERENCE_SENSOR] = "reference_not_a_room"
    return errors


class RoomThermostatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pick the thermostat to steer and the rooms to steer it by."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RoomThermostatOptionsFlow:
        return RoomThermostatOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            climate = user_input[CONF_CLIMATE_ENTITY]
            await self.async_set_unique_id(climate)
            self._abort_if_unique_id_configured()
            errors = validate_rooms(user_input)
            entry = er.async_get(self.hass).async_get(climate)
            if entry is not None and entry.platform == DOMAIN:
                errors[CONF_CLIMATE_ENTITY] = "cannot_wrap_itself"
            if not errors:
                options = default_options(self.hass) | {
                    CONF_ROOM_SENSORS: user_input[CONF_ROOM_SENSORS],
                    **({CONF_REFERENCE_SENSOR: ref} if (ref := user_input.get(CONF_REFERENCE_SENSOR)) else {}),
                }
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data={CONF_CLIMATE_ENTITY: climate}, options=options
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
                vol.Required(CONF_CLIMATE_ENTITY): EntitySelector(EntitySelectorConfig(domain=CLIMATE_DOMAIN)),
                vol.Required(CONF_ROOM_SENSORS): ROOM_SENSOR_SELECTOR,
                vol.Optional(CONF_REFERENCE_SENSOR): REFERENCE_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}), errors=errors
        )


class RoomThermostatOptionsFlow(OptionsFlowWithReload):
    """Rooms, schedule and every tunable; saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = dict(self.config_entry.options)
        if user_input is not None:
            errors = validate_rooms(user_input)
            default_room = user_input.get(CONF_DEFAULT_ROOM)
            valid_defaults = {
                *build_rooms(user_input.get(CONF_ROOM_SENSORS) or []),
                *(s.value for s in (Strategy.AVERAGE, Strategy.EXTREME)),
            }
            if default_room and default_room not in valid_defaults:
                errors[CONF_DEFAULT_ROOM] = "unknown_default_room"
            if not errors:
                return self.async_create_entry(data=user_input)
            current = user_input

        rooms = build_rooms(current.get(CONF_ROOM_SENSORS) or [])
        room_choices = [SelectOptionDict(value=room_id, label=room_id) for room_id in rooms]
        room_choices += [SelectOptionDict(value=s.value, label=s.value) for s in (Strategy.AVERAGE, Strategy.EXTREME)]

        def number(minimum: float, maximum: float, step: float) -> NumberSelector:
            return NumberSelector(
                NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_SENSORS): ROOM_SENSOR_SELECTOR,
                vol.Optional(CONF_REFERENCE_SENSOR): REFERENCE_SELECTOR,
                vol.Optional(CONF_DEFAULT_ROOM): SelectSelector(
                    SelectSelectorConfig(options=room_choices, mode=SelectSelectorMode.DROPDOWN, custom_value=True)
                ),
                vol.Optional(CONF_SCHEDULE_ENTITY): EntitySelector(EntitySelectorConfig(domain="schedule")),
                vol.Required(CONF_MANUAL_CHANGE_POLICY): SelectSelector(
                    SelectSelectorConfig(
                        options=[policy.value for policy in ManualChangePolicy],
                        translation_key=CONF_MANUAL_CHANGE_POLICY,
                    )
                ),
                vol.Required(CONF_HOLD_DURATION): number(0, 1440, 5),
                vol.Required(CONF_STALE_AFTER): number(1, 240, 1),
                vol.Required(CONF_MAX_OFFSET): number(0, 20, 0.1),
                vol.Required(CONF_DEADBAND): number(0, 5, 0.1),
                vol.Required(CONF_MIN_WRITE_INTERVAL): number(0, 240, 1),
                vol.Required(CONF_SMOOTHING): number(0, 240, 1),
                vol.Required(CONF_SETPOINT_STEP): number(0, 5, 0.1),
                vol.Required(CONF_MINIMUM_RANGE): number(0, 20, 0.1),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, default_options(self.hass) | current),
            errors=errors,
        )
