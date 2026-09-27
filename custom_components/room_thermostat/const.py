"""Constants for the Room Thermostat integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform, UnitOfTemperature

DOMAIN: Final = "room_thermostat"

# Config entry data (fixed once created).
CONF_CLIMATE_ENTITY: Final = "climate_entity"

# Config entry options (editable in Configure).
CONF_ROOM_SENSORS: Final = "room_sensors"
CONF_REFERENCE_SENSOR: Final = "reference_sensor"
CONF_DEFAULT_ROOM: Final = "default_room"
CONF_SCHEDULE_ENTITY: Final = "schedule_entity"
CONF_STALE_AFTER: Final = "stale_after_minutes"
CONF_MAX_OFFSET: Final = "max_offset"
CONF_DEADBAND: Final = "deadband"
CONF_MIN_WRITE_INTERVAL: Final = "min_write_interval_minutes"
CONF_SMOOTHING: Final = "smoothing_minutes"
CONF_SETPOINT_STEP: Final = "setpoint_step"
CONF_MINIMUM_RANGE: Final = "minimum_range"
CONF_MANUAL_CHANGE_POLICY: Final = "manual_change_policy"
CONF_HOLD_DURATION: Final = "hold_duration_minutes"

#: Schedule block data keys.
BLOCK_ROOM: Final = "room"
BLOCK_HEAT: Final = "heat"
BLOCK_COOL: Final = "cool"

#: Fired when the thermostat is changed outside Home Assistant.
EVENT_MANUAL_CHANGE: Final = "room_thermostat_manual_change"
#: Fired when a manual hold ends; `reason` is expired, resumed or schedule.
EVENT_HOLD_ENDED: Final = "room_thermostat_hold_ended"

#: How often freshness is re-checked when nothing else changes.
EVALUATE_INTERVAL_SECONDS: Final = 60

DEFAULT_STALE_AFTER: Final = 10
DEFAULT_MIN_WRITE_INTERVAL: Final = 15
DEFAULT_SMOOTHING: Final = 15
#: Minutes a change made outside Home Assistant is respected before control resumes (0 = indefinitely).
DEFAULT_HOLD_DURATION: Final = 120

#: Unit-dependent defaults: (max offset, deadband, setpoint step).
DEFAULTS_BY_UNIT: Final[dict[str, tuple[float, float, float]]] = {
    UnitOfTemperature.FAHRENHEIT: (6.0, 0.5, 0.5),
    UnitOfTemperature.CELSIUS: (3.5, 0.3, 0.5),
}

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
]
