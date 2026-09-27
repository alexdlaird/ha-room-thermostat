"""The room thermostat: shows the room's temperature and takes room targets in every mode.

Modes, action and limits mirror the underlying thermostat; mode changes pass straight through to
it. Setpoints set here are room targets, which the controller translates into thermostat setpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import DOMAIN
from .control import LiveMode
from .entity import RoomThermostatEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import RoomThermostatConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomThermostatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RoomClimate(entry.runtime_data, None)])


def _hvac_mode(value: Any) -> HVACMode | None:
    try:
        return HVACMode(value)
    except ValueError:
        return None


class RoomClimate(RoomThermostatEntity, ClimateEntity):
    """The Nest-style thermostat for the room that matters."""

    _attr_name = None
    _attr_translation_key = "room_thermostat"
    _attr_precision = PRECISION_TENTHS

    @property
    def _underlying(self) -> State | None:
        return self.hass.states.get(self.controller.climate_entity_id)

    @property
    def temperature_unit(self) -> str:
        return self.controller.unit

    @property
    def target_temperature_step(self) -> float:
        return self.controller.settings.setpoint_step

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        mode = self.controller.snapshot.mode
        if mode in (LiveMode.HEAT, LiveMode.COOL):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        elif mode is LiveMode.HEAT_COOL:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return features

    @property
    def hvac_modes(self) -> list[HVACMode]:
        state = self._underlying
        modes = state.attributes.get(ATTR_HVAC_MODES, []) if state else []
        parsed = [mode for mode in (_hvac_mode(value) for value in modes) if mode is not None]
        return parsed or [HVACMode.OFF]

    @property
    def hvac_mode(self) -> HVACMode | None:
        state = self._underlying
        return _hvac_mode(state.state) if state else None

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self._underlying
        value = state.attributes.get(ATTR_HVAC_ACTION) if state else None
        return HVACAction(value) if value in {action.value for action in HVACAction} else None

    @property
    def current_temperature(self) -> float | None:
        return self.controller.snapshot.room_temperature

    @property
    def target_temperature(self) -> float | None:
        mode = self.controller.snapshot.mode
        if mode is LiveMode.HEAT:
            return self.controller.targets.heat
        if mode is LiveMode.COOL:
            return self.controller.targets.cool
        return None

    @property
    def target_temperature_low(self) -> float | None:
        return self.controller.targets.heat if self.controller.snapshot.mode is LiveMode.HEAT_COOL else None

    @property
    def target_temperature_high(self) -> float | None:
        return self.controller.targets.cool if self.controller.snapshot.mode is LiveMode.HEAT_COOL else None

    @property
    def min_temp(self) -> float:
        return self._limit(ATTR_MIN_TEMP, DEFAULT_MIN_TEMP)

    @property
    def max_temp(self) -> float:
        return self._limit(ATTR_MAX_TEMP, DEFAULT_MAX_TEMP)

    def _limit(self, attribute: str, default_celsius: float) -> float:
        state = self._underlying
        value = state.attributes.get(attribute) if state else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return TemperatureConverter.convert(default_celsius, UnitOfTemperature.CELSIUS, self.controller.unit)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.controller.snapshot
        names = self.controller.room_names
        return {
            "control_state": snapshot.state.value,
            "active_rooms": [names.get(room_id, room_id) for room_id in snapshot.room_ids],
            "offset": snapshot.offset,
            "commanded_heat": snapshot.commanded.heat,
            "commanded_cool": snapshot.commanded.cool,
            "thermostat": self.controller.climate_entity_id,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.controller.async_set_hvac_mode(hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Room targets; an `hvac_mode` in the same call is applied to the thermostat first."""
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        low, high = kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        mode = self.controller.snapshot.mode if hvac_mode is None else _live(hvac_mode)
        if low is None and high is None and temperature is not None and mode not in (LiveMode.HEAT, LiveMode.COOL):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="single_target_not_applicable")
        if hvac_mode is not None:
            await self.controller.async_set_hvac_mode(hvac_mode)
        if low is not None or high is not None:
            await self.controller.async_set_targets(heat=low, cool=high)
        elif temperature is not None and mode is LiveMode.HEAT:
            await self.controller.async_set_targets(heat=temperature)
        elif temperature is not None:
            await self.controller.async_set_targets(cool=temperature)


def _live(hvac_mode: HVACMode) -> LiveMode:
    return {HVACMode.HEAT: LiveMode.HEAT, HVACMode.COOL: LiveMode.COOL, HVACMode.HEAT_COOL: LiveMode.HEAT_COOL}.get(
        hvac_mode, LiveMode.NONE
    )
