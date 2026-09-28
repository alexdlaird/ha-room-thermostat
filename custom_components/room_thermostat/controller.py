"""Runtime glue: reads Home Assistant state into the control rules and writes the thermostat.

One controller per config entry. It listens to the underlying climate entity, the room sensors and
the optional schedule, re-evaluates on every change (and once a minute, so staleness is noticed),
and calls `climate.set_temperature` on the underlying thermostat when the rules say so. Everything
the entities show comes from `snapshot`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter
import voluptuous as vol

from .const import (
    BLOCK_COOL,
    BLOCK_HEAT,
    BLOCK_ROOM,
    CONF_CLIMATE_ENTITY,
    CONF_DEADBAND,
    CONF_DEFAULT_ROOM,
    CONF_FAN_SPEED_ENTITY,
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
    DEFAULT_HOLD_DURATION,
    DEFAULT_MIN_WRITE_INTERVAL,
    DEFAULT_SMOOTHING,
    DEFAULT_STALE_AFTER,
    DEFAULTS_BY_UNIT,
    DOMAIN,
    EVALUATE_INTERVAL_SECONDS,
    EVENT_HOLD_ENDED,
    EVENT_MANUAL_CHANGE,
    EVENT_OVERRIDE_ENDED,
    PRESET_DEFAULTS_BY_UNIT,
)
from .control import (
    ControlState,
    LiveMode,
    ManualChangePolicy,
    OffsetFilter,
    RoomReading,
    ScheduleStatus,
    Selection,
    Setpoints,
    Settings,
    Strategy,
    adopted_targets,
    choose_rooms,
    commanded_setpoints,
    effective_offset,
    fresh_readings,
    is_external_change,
    mean,
    needs_write,
    parse_block,
    parse_selection,
    room_error,
    write_allowed,
)
from .planner import (
    EMPTY_SCHEDULE,
    Hold,
    HoldKind,
    PlanError,
    Preset,
    Schedule,
    active_block,
    default_presets,
    hold_from_dict,
    hold_to_dict,
    missing_presets,
    next_block_start,
    parse_presets,
    parse_schedule,
    preset_to_dict,
    presets_from_storage,
    schedule_from_storage,
    schedule_to_list,
    selection_to_str,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
LIVE_MODE_BY_STATE: Final[dict[str, LiveMode]] = {
    HVACMode.HEAT: LiveMode.HEAT,
    HVACMode.COOL: LiveMode.COOL,
    HVACMode.HEAT_COOL: LiveMode.HEAT_COOL,
}
#: Room targets used when neither storage nor the thermostat offers any: (heat, cool) per unit.
FALLBACK_TARGETS: Final[dict[str, tuple[float, float]]] = {
    UnitOfTemperature.FAHRENHEIT: (68.0, 74.0),
    UnitOfTemperature.CELSIUS: (20.0, 23.5),
}
TEMPERATURE_UNITS: Final = {unit.value for unit in UnitOfTemperature}
PROBLEM_BY_STATE: Final[dict[ControlState, str]] = {
    ControlState.FALLBACK_REFERENCE: "active_room_stale",
    ControlState.FALLBACK_THERMOSTAT: "no_fresh_room",
    ControlState.UNDERLYING_UNAVAILABLE: "thermostat_unavailable",
}


@dataclass(frozen=True)
class Snapshot:
    """What the controller last decided, for the entities to show."""

    state: ControlState = ControlState.UNDERLYING_UNAVAILABLE
    mode: LiveMode = LiveMode.NONE
    room_ids: tuple[str, ...] = ()
    room_temperature: float | None = None
    offset: float | None = None
    commanded: Setpoints = field(default_factory=Setpoints)
    error: float | None = None
    problems: tuple[str, ...] = ()
    hold_until: datetime | None = None
    schedule: ScheduleStatus = ScheduleStatus.NOT_CONFIGURED
    #: When the schedule next starts or ends a block (from the schedule helper's `next_event`).
    schedule_next_change: datetime | None = None
    #: The hold set through Home Assistant (dial, preset, app), if any.
    override: Hold | None = None
    #: The real thermostat's own reading (unit-converted), for history next to the rooms.
    thermostat_temperature: float | None = None


def room_id_for(entity_id: str) -> str:
    """`sensor.living_room_temperature` -> `living_room`."""
    object_id = entity_id.split(".", 1)[1]
    return object_id.removesuffix("_temperature") or object_id


def build_rooms(entity_ids: list[str]) -> dict[str, str]:
    """Room id -> sensor entity id, keeping ids unique."""
    rooms: dict[str, str] = {}
    for entity_id in entity_ids:
        base = room_id = room_id_for(entity_id)
        suffix = 2
        while room_id in rooms:
            room_id = f"{base}_{suffix}"
            suffix += 1
        rooms[room_id] = entity_id
    return rooms


def unit_defaults(unit: str) -> tuple[float, float, float]:
    return DEFAULTS_BY_UNIT.get(unit, DEFAULTS_BY_UNIT[UnitOfTemperature.CELSIUS])


def live_mode(state: State | None) -> LiveMode:
    return LiveMode.NONE if state is None else LIVE_MODE_BY_STATE.get(state.state, LiveMode.NONE)


def current_setpoints(state: State | None, mode: LiveMode) -> Setpoints:
    """The setpoints the underlying thermostat reports for its mode."""
    if state is None:
        return Setpoints()
    attributes = state.attributes
    if mode is LiveMode.HEAT:
        return Setpoints(heat=_float(attributes.get(ATTR_TEMPERATURE)))
    if mode is LiveMode.COOL:
        return Setpoints(cool=_float(attributes.get(ATTR_TEMPERATURE)))
    if mode is LiveMode.HEAT_COOL:
        return Setpoints(_float(attributes.get(ATTR_TARGET_TEMP_LOW)), _float(attributes.get(ATTR_TARGET_TEMP_HIGH)))
    return Setpoints()


def _limit(value: Any, default: float) -> float:
    number = _float(value)
    return default if number is None else number


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


class RoomThermostatController:
    """Owns the control state of one room thermostat."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        options = entry.options
        self.unit = hass.config.units.temperature_unit
        max_offset, deadband, step = unit_defaults(self.unit)
        self.climate_entity_id: str = entry.data[CONF_CLIMATE_ENTITY]
        self.rooms = build_rooms(list(options.get(CONF_ROOM_SENSORS, [])))
        unserved = set(options.get(CONF_UNSERVED_ROOMS) or [])
        #: Rooms this thermostat does not heat or cool: reported, never followed.
        self.unserved = {room_id for room_id, entity_id in self.rooms.items() if entity_id in unserved}
        reference = options.get(CONF_REFERENCE_SENSOR)
        self.reference_id = next(
            (rid for rid, eid in self.rooms.items() if eid == reference and rid not in self.unserved), None
        )
        self.schedule_entity_id: str | None = options.get(CONF_SCHEDULE_ENTITY) or None
        self.outdoor_entity_id: str | None = options.get(CONF_OUTDOOR_SENSOR) or None
        self.fan_speed_entity_id: str | None = options.get(CONF_FAN_SPEED_ENTITY) or None
        self.settings = Settings(
            stale_after=timedelta(minutes=float(options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER))),
            max_offset=float(options.get(CONF_MAX_OFFSET, max_offset)),
            deadband=float(options.get(CONF_DEADBAND, deadband)),
            min_write_interval=timedelta(
                minutes=float(options.get(CONF_MIN_WRITE_INTERVAL, DEFAULT_MIN_WRITE_INTERVAL))
            ),
            smoothing=timedelta(minutes=float(options.get(CONF_SMOOTHING, DEFAULT_SMOOTHING))),
            setpoint_step=float(options.get(CONF_SETPOINT_STEP, step)),
            minimum_range=float(options.get(CONF_MINIMUM_RANGE, 0.0)),
        )
        self.policy = ManualChangePolicy(options.get(CONF_MANUAL_CHANGE_POLICY, ManualChangePolicy.HOLD))
        #: None holds until the next schedule block or Resume.
        hold_minutes = float(options.get(CONF_HOLD_DURATION, DEFAULT_HOLD_DURATION))
        self.hold_duration: timedelta | None = timedelta(minutes=hold_minutes) if hold_minutes > 0 else None
        self.default_selection = parse_selection(options.get(CONF_DEFAULT_ROOM), self.followable_rooms) or (
            Selection.room(self.reference_id) if self.reference_id else Selection(Strategy.AVERAGE)
        )

        self.selection = self.default_selection
        self.targets = Setpoints()
        self.hold = False
        self.hold_until: datetime | None = None
        self.filters: dict[str, OffsetFilter] = {}
        self.block_signature: list[Any] | None = None
        self.presets: dict[str, Preset] = {}
        self.schedule: Schedule = EMPTY_SCHEDULE
        self.active_preset: str | None = None
        self.override: Hold | None = None
        #: What to return to when a timed hold ends and no schedule block applies: targets, selection, preset.
        self.baseline: tuple[Setpoints, Selection, str | None] | None = None
        #: (weekday, index, start, preset) of the internal schedule block last applied.
        self.block_key: list[Any] | None = None
        #: Bumped whenever presets or the schedule change, so clients know to re-read them.
        self.config_revision = 0
        self.last_commanded: Setpoints | None = None
        self.last_write_at: datetime | None = None
        self.snapshot = Snapshot()

        self._previous_mode: LiveMode | None = None
        self._previous_setpoints: Setpoints | None = None
        self._urgent = True
        self._block_errors: tuple[str, ...] = ()
        self._write_error: str | None = None
        self._write_lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Restore state, follow the tracked entities, and run a first evaluation."""
        self._restore(await self._store.async_load() or {})
        underlying = self.hass.states.get(self.climate_entity_id)
        if self.targets.heat is None or self.targets.cool is None:
            self.targets = self._initial_targets(underlying)
        self._ensure_built_in_presets()
        self._previous_mode = live_mode(underlying)
        self._previous_setpoints = current_setpoints(underlying, self._previous_mode)

        tracked = [self.climate_entity_id, *self.rooms.values()]
        if self.schedule_entity_id:
            tracked.append(self.schedule_entity_id)
        self.entry.async_on_unload(async_track_state_change_event(self.hass, tracked, self._async_on_state_change))
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._async_on_tick, timedelta(seconds=EVALUATE_INTERVAL_SECONDS))
        )
        if not self.uses_internal_schedule:
            self._apply_schedule_if_changed()
        self.evaluate()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @property
    def room_names(self) -> dict[str, str]:
        """Room id -> display name (the sensor's name without a trailing "Temperature")."""
        names: dict[str, str] = {}
        for room_id, entity_id in self.rooms.items():
            state = self.hass.states.get(entity_id)
            name = str(state.attributes.get(ATTR_FRIENDLY_NAME, "")) if state else ""
            name = name.strip()
            if name.casefold().endswith(" temperature"):
                name = name[: -len(" temperature")].strip()
            names[room_id] = name or room_id.replace("_", " ").title()
        return names

    @property
    def followable_rooms(self) -> dict[str, str]:
        """Room id -> display name for the rooms this thermostat can steer by."""
        return {room_id: name for room_id, name in self.room_names.items() if room_id not in self.unserved}

    # ------------------------------------------------------------------- inputs

    @callback
    def _async_on_tick(self, _now: datetime) -> None:
        self.evaluate()
        self._save()

    @callback
    def _async_on_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self.climate_entity_id:
            self._observe_underlying(event.data["new_state"])
        elif entity_id == self.schedule_entity_id and not self.uses_internal_schedule:
            self._apply_schedule_if_changed()
        self.evaluate()

    def _observe_underlying(self, state: State | None) -> None:
        """Notice mode changes and setpoint changes nobody asked this controller for."""
        mode = live_mode(state)
        current = current_setpoints(state, mode)
        if mode is not self._previous_mode:
            self._urgent = True
        elif mode is not LiveMode.NONE and is_external_change(self._previous_setpoints, current, self.last_commanded):
            self._handle_manual_change(mode, current)
        self._previous_mode = mode
        self._previous_setpoints = current

    def _handle_manual_change(self, mode: LiveMode, current: Setpoints) -> None:
        if self.policy is ManualChangePolicy.ADOPT:
            self.targets = adopted_targets(mode, current, self.snapshot.offset or 0.0, self.targets)
        else:
            # A further change during a hold restarts the clock: the latest setting gets the full window.
            self.hold = True
            self.hold_until = None if self.hold_duration is None else dt_util.utcnow() + self.hold_duration
        _LOGGER.info("%s changed outside Home Assistant (%s): %s", self.climate_entity_id, self.policy, current)
        self.hass.bus.async_fire(
            EVENT_MANUAL_CHANGE,
            {
                "entry_id": self.entry.entry_id,
                "thermostat": self.climate_entity_id,
                "policy": self.policy.value,
                "mode": mode.value,
                "heat": current.heat,
                "cool": current.cool,
                "hold_until": None if self.hold_until is None else self.hold_until.isoformat(),
            },
        )
        self._save()

    def _schedule_signature(self) -> list[Any] | None:
        if not self.schedule_entity_id:
            return None
        state = self.hass.states.get(self.schedule_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        attributes = state.attributes
        return [state.state, *(attributes.get(key) for key in (BLOCK_ROOM, BLOCK_HEAT, BLOCK_COOL))]

    def _apply_schedule_if_changed(self) -> None:
        """A new schedule block (or leaving one) sets room and targets and ends any hold."""
        signature = self._schedule_signature()
        if signature is None or signature == self.block_signature:
            return
        self.block_signature = signature
        state, room, heat, cool = signature
        if state == STATE_ON:
            data = {
                key: value
                for key, value in ((BLOCK_ROOM, room), (BLOCK_HEAT, heat), (BLOCK_COOL, cool))
                if value is not None
            }
            block = parse_block(data, self.followable_rooms)
            errors = list(block.errors)
            if block.selection is not None:
                self.selection = block.selection
            new_heat = block.heat if block.heat is not None else self.targets.heat
            new_cool = block.cool if block.cool is not None else self.targets.cool
            if new_heat is not None and new_cool is not None and new_cool - new_heat < self.settings.minimum_range:
                errors.append("heat and cool are closer than the minimum range")
                new_cool = new_heat + self.settings.minimum_range
            self.targets = Setpoints(new_heat, new_cool)
            self._block_errors = tuple(errors)
        else:
            self.selection = self.default_selection
            self._block_errors = ()
        self._end_hold("schedule")
        self._end_override("schedule")
        self._urgent = True
        self._save()

    @property
    def uses_internal_schedule(self) -> bool:
        """The schedule edited through Home Assistant (presets per time of day); it replaces a schedule helper."""
        return any(self.schedule)

    def _apply_internal_schedule_if_changed(self, now: datetime) -> None:
        """A new block applies its preset and ends a next-block hold; timed and indefinite holds outrank it."""
        if not self.uses_internal_schedule:
            self.block_key = None
            return
        found = active_block(self.schedule, dt_util.as_local(now))
        if found is None:  # pragma: no cover - a non-empty schedule always has a block in effect
            return
        weekday, index, block = found
        key = [weekday, index, block.start.strftime("%H:%M"), block.preset_id]
        if key == self.block_key:
            return
        self.block_key = key
        if self.override is not None and self.override.kind is not HoldKind.NEXT_BLOCK:
            self._save()
            return
        self._apply_preset(self.presets[block.preset_id])
        self._end_hold("schedule")
        self._end_override("schedule")
        self._urgent = True
        self._save()

    def _apply_preset(self, preset: Preset) -> None:
        self.targets = preset.targets
        self.selection = preset.selection
        self.active_preset = preset.preset_id

    def _return_to_plan(self) -> None:
        """After a hold: the schedule's current preset, else whatever was in effect before the hold."""
        if self.uses_internal_schedule:
            self.block_key = None
            self._apply_internal_schedule_if_changed(dt_util.utcnow())
        elif self.baseline is not None:
            self.targets, self.selection, self.active_preset = self.baseline
        self.baseline = None

    # --------------------------------------------------------------- evaluation

    @callback
    def evaluate(self) -> None:
        """Recompute everything and write the thermostat when the rules say so."""
        now = dt_util.utcnow()
        if self.hold and self.hold_until is not None and now >= self.hold_until:
            self._end_hold("expired")
            self._urgent = True
            self._save()
        if self.override is not None and self.override.expired(now):
            self._end_override("expired")
            self._return_to_plan()
            self._urgent = True
            self._save()
        self._apply_internal_schedule_if_changed(now)
        underlying = self.hass.states.get(self.climate_entity_id)
        thermostat_temperature = (
            None
            if underlying is None or underlying.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else _float(underlying.attributes.get(ATTR_CURRENT_TEMPERATURE))
        )
        if underlying is None or thermostat_temperature is None:
            self._publish(Snapshot(problems=self._problems(ControlState.UNDERLYING_UNAVAILABLE)))
            return

        mode = live_mode(underlying)
        fresh = fresh_readings(self._readings(), now, self.settings.stale_after)
        fresh = {room_id: value for room_id, value in fresh.items() if room_id not in self.unserved}
        for room_id, temperature in fresh.items():
            self.filters.setdefault(room_id, OffsetFilter()).update(
                thermostat_temperature - temperature, now, self.settings.smoothing
            )
        choice = choose_rooms(self.selection, fresh, self.reference_id, mode, self.targets)
        offset = effective_offset(choice, self.filters, self.reference_id, self.settings.max_offset)
        room_temperature = mean(fresh[room_id] for room_id in choice.room_ids)
        if room_temperature is None:
            room_temperature = thermostat_temperature - offset
        attributes = underlying.attributes
        commanded = commanded_setpoints(
            mode,
            self.targets,
            offset,
            minimum=_limit(attributes.get(ATTR_MIN_TEMP), float("-inf")),
            maximum=_limit(attributes.get(ATTR_MAX_TEMP), float("inf")),
            settings=self.settings,
        )
        if mode is LiveMode.NONE:
            state = ControlState.IDLE
        elif self.hold:
            state = ControlState.MANUAL_HOLD
        else:
            state = choice.state

        self._publish(
            Snapshot(
                state=state,
                mode=mode,
                room_ids=choice.room_ids,
                room_temperature=round(room_temperature, 2),
                offset=round(offset, 2),
                commanded=commanded,
                error=room_error(mode, room_temperature, self.targets),
                problems=self._problems(choice.state),
                hold_until=self.hold_until if self.hold else None,
                thermostat_temperature=thermostat_temperature,
            )
        )

        if (
            mode is not LiveMode.NONE
            and not self.hold
            and not self._write_lock.locked()
            and needs_write(commanded, current_setpoints(underlying, mode), self.settings.deadband)
            and write_allowed(now, self.last_write_at, self.settings.min_write_interval, urgent=self._urgent)
        ):
            self.hass.async_create_task(self._async_write(mode, commanded), eager_start=True)

    def _readings(self) -> dict[str, RoomReading | None]:
        return {room_id: self._reading(entity_id) for room_id, entity_id in self.rooms.items()}

    def _reading(self, entity_id: str) -> RoomReading | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = _float(state.state)
        if value is None:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit in TEMPERATURE_UNITS and unit != self.unit:
            value = TemperatureConverter.convert(value, unit, self.unit)
        return RoomReading(value, state.last_reported)

    def _schedule_status(self) -> tuple[ScheduleStatus, datetime | None]:
        if self.uses_internal_schedule:
            return ScheduleStatus.IN_BLOCK, next_block_start(self.schedule, dt_util.now())
        if not self.schedule_entity_id:
            return ScheduleStatus.NOT_CONFIGURED, None
        state = self.hass.states.get(self.schedule_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ScheduleStatus.NOT_FOUND, None
        next_event = state.attributes.get("next_event")
        if isinstance(next_event, str):
            next_event = dt_util.parse_datetime(next_event)
        status = ScheduleStatus.IN_BLOCK if state.state == STATE_ON else ScheduleStatus.BETWEEN_BLOCKS
        return status, next_event if isinstance(next_event, datetime) else None

    def _problems(self, choice_state: ControlState) -> tuple[str, ...]:
        problems = [PROBLEM_BY_STATE[choice_state]] if choice_state in PROBLEM_BY_STATE else []
        if self._schedule_status()[0] is ScheduleStatus.NOT_FOUND:
            problems.append(f"schedule {self.schedule_entity_id} not found")
        problems.extend(f"schedule: {error}" for error in self._block_errors)
        if self._write_error is not None:
            problems.append(f"write failed: {self._write_error}")
        return tuple(problems)

    def _publish(self, snapshot: Snapshot) -> None:
        status, next_change = self._schedule_status()
        self.snapshot = replace(snapshot, schedule=status, schedule_next_change=next_change, override=self.override)
        for update in list(self._listeners):
            update()

    async def _async_write(self, mode: LiveMode, commanded: Setpoints) -> None:
        async with self._write_lock:
            data: dict[str, Any] = {ATTR_ENTITY_ID: self.climate_entity_id}
            if mode is LiveMode.HEAT_COOL:
                data[ATTR_TARGET_TEMP_LOW] = commanded.heat
                data[ATTR_TARGET_TEMP_HIGH] = commanded.cool
            else:
                data[ATTR_TEMPERATURE] = commanded.heat if mode is LiveMode.HEAT else commanded.cool
            self.last_write_at = dt_util.utcnow()
            self.last_commanded = commanded
            self._urgent = False
            try:
                await self.async_call_thermostat(SERVICE_SET_TEMPERATURE, data)
            except (HomeAssistantError, vol.Invalid) as err:
                self._write_error = str(err) or type(err).__name__
                _LOGGER.warning("Could not set %s to %s: %s", self.climate_entity_id, commanded, self._write_error)
            else:
                self._write_error = None
            self._save()
        self.evaluate()

    # --------------------------------------------------------- user-facing setters

    async def async_set_targets(
        self, heat: float | None = None, cool: float | None = None, hold: Hold | None = None
    ) -> None:
        """New room targets; by default they stand until the next schedule block."""
        new = Setpoints(self.targets.heat if heat is None else heat, self.targets.cool if cool is None else cool)
        if (
            heat is not None
            and cool is not None
            and new.heat is not None
            and new.cool is not None
            and new.cool - new.heat < self.settings.minimum_range
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="range_too_narrow",
                translation_placeholders={"minimum": f"{self.settings.minimum_range:g}"},
            )
        if heat is not None and cool is None and new.cool is not None and new.heat is not None:
            new = Setpoints(new.heat, max(new.cool, new.heat + self.settings.minimum_range))
        if cool is not None and heat is None and new.cool is not None and new.heat is not None:
            new = Setpoints(min(new.heat, new.cool - self.settings.minimum_range), new.cool)
        self._start_override(hold)
        self.targets = new
        self.active_preset = None
        self._resume()

    async def async_set_selection(self, selection: Selection, hold: Hold | None = None) -> None:
        self._start_override(hold)
        self.selection = selection
        self.active_preset = None
        self._resume()

    async def async_activate_preset(self, preset_id: str, hold: Hold | None = None) -> None:
        """Switch to a preset; by default it stands until the next schedule block."""
        preset = self.presets.get(preset_id)
        if preset is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_preset",
                translation_placeholders={"preset": preset_id},
            )
        self._start_override(hold, preset=True)
        self._apply_preset(preset)
        self._resume()

    def preset_by_name(self, name: str) -> Preset | None:
        folded = name.casefold()
        return next((preset for preset in self.presets.values() if preset.name.casefold() == folded), None)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Pass a mode change through to the underlying thermostat."""
        await self.async_call_thermostat(
            SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: hvac_mode}
        )
        self._resume()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Pass a fan mode (e.g. circulation) through to the underlying thermostat."""
        await self.async_call_thermostat(
            SERVICE_SET_FAN_MODE, {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_FAN_MODE: fan_mode}
        )

    async def async_call_thermostat(self, service: str, data: dict[str, Any]) -> None:
        """Every call to the underlying thermostat goes through here."""
        await self.hass.services.async_call(CLIMATE_DOMAIN, service, data, blocking=True, context=Context())

    async def async_resume(self) -> None:
        """Back to the plan: ends any hold and returns to the schedule's preset (or what ran before the hold)."""
        self._end_override("resumed")
        self._return_to_plan()
        self._resume()

    def async_save_presets(self, data: Any) -> None:
        """Replace the preset list (built-ins stay; presets the schedule uses cannot be removed)."""
        presets = parse_presets(data, self.followable_rooms, self.settings.minimum_range, self.presets)
        in_use = missing_presets(presets, self.schedule)
        if in_use:
            raise PlanError(f"the schedule still uses {', '.join(self.presets[p].name for p in in_use)}")
        self.presets = presets
        if self.active_preset not in presets:
            self.active_preset = None
        else:
            self._apply_preset(presets[self.active_preset])
        self._config_changed()

    def async_save_schedule(self, data: Any) -> None:
        """Replace the weekly schedule; the block now in effect applies unless a hold outranks it."""
        self.schedule = parse_schedule(data, self.presets)
        self.block_key = None
        self._config_changed()

    def config(self) -> dict[str, Any]:
        """What a client needs to show and edit presets and the schedule."""
        return {
            "revision": self.config_revision,
            "unit": self.unit,
            "setpoint_step": self.settings.setpoint_step,
            "minimum_range": self.settings.minimum_range,
            "rooms": [
                {
                    "id": room_id,
                    "name": name,
                    "entity_id": self.rooms[room_id],
                    "followable": room_id not in self.unserved,
                }
                for room_id, name in self.room_names.items()
            ],
            "history": self._history_entities(),
            "controls": {"fan_speed": self.fan_speed_entity_id},
            "presets": [preset_to_dict(preset) for preset in self.presets.values()],
            "schedule": schedule_to_list(self.schedule),
            "active_preset": self.active_preset,
            "hold": hold_to_dict(self.override),
            "selection": selection_to_str(self.selection),
        }

    def _history_entities(self) -> dict[str, str | None]:
        """Entity ids an app charts next to the rooms: this thermostat's own series plus the optional outdoor sensor."""
        registry = er.async_get(self.hass)

        def own(key: str) -> str | None:
            return registry.async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}-{key}")

        return {
            "room_temperature": own("room_temperature"),
            "thermostat_temperature": own("thermostat_temperature"),
            "commanded_heat": own("commanded_heat"),
            "commanded_cool": own("commanded_cool"),
            "outdoor_temperature": self.outdoor_entity_id,
            "thermostat": self.climate_entity_id,
        }

    def _config_changed(self) -> None:
        self.config_revision += 1
        self._urgent = True
        self._save()
        self.evaluate()

    def _start_override(self, hold: Hold | None, *, preset: bool = False) -> None:
        """A change through Home Assistant outranks the plan.

        Without any schedule, a new target or room is simply the new setting, a timed hold returns to the previous
        one, and a preset is held until resumed, so presets work as toggles over the normal setting.
        """
        hold = hold or Hold(HoldKind.NEXT_BLOCK)
        has_plan = self.uses_internal_schedule or self.schedule_entity_id is not None
        if not has_plan and hold.kind is not HoldKind.UNTIL:
            if not preset:
                self.override = None
                self.baseline = None
                return
            hold = Hold(HoldKind.INDEFINITE)
        if self.override is None:
            self.baseline = (self.targets, self.selection, self.active_preset)
        self.override = hold

    def _end_override(self, reason: str) -> None:
        if self.override is not None:
            self.hass.bus.async_fire(EVENT_OVERRIDE_ENDED, {"entry_id": self.entry.entry_id, "reason": reason})
            if reason == "schedule":
                self.baseline = None
        self.override = None

    def _end_hold(self, reason: str) -> None:
        """Leave a manual hold (if in one), telling listeners why."""
        if self.hold:
            self.hass.bus.async_fire(EVENT_HOLD_ENDED, {"entry_id": self.entry.entry_id, "reason": reason})
        self.hold = False
        self.hold_until = None

    def _resume(self) -> None:
        self._end_hold("resumed")
        self._urgent = True
        self._save()
        self.evaluate()

    # -------------------------------------------------------------- persistence

    def _initial_targets(self, state: State | None) -> Setpoints:
        heat_default, cool_default = FALLBACK_TARGETS.get(self.unit, FALLBACK_TARGETS[UnitOfTemperature.CELSIUS])
        gap = max(cool_default - heat_default, self.settings.minimum_range)
        current = current_setpoints(state, live_mode(state))
        heat = self.targets.heat if self.targets.heat is not None else current.heat
        cool = self.targets.cool if self.targets.cool is not None else current.cool
        if heat is None and cool is None:
            return Setpoints(heat_default, cool_default)
        if heat is None:
            assert cool is not None
            heat = cool - gap
        if cool is None:
            cool = heat + gap
        return Setpoints(heat, cool)

    def _ensure_built_in_presets(self) -> None:
        """New installs (and upgrades) get Home (today's targets), Away and Sleep."""
        (away_heat, away_cool), (sleep_heat, sleep_cool) = PRESET_DEFAULTS_BY_UNIT.get(
            self.unit, PRESET_DEFAULTS_BY_UNIT[UnitOfTemperature.CELSIUS]
        )
        defaults = default_presets(
            self.targets, self.default_selection, Setpoints(away_heat, away_cool), Setpoints(sleep_heat, sleep_cool)
        )
        for preset_id, preset in defaults.items():
            self.presets.setdefault(preset_id, preset)

    def _restore(self, stored: Mapping[str, Any]) -> None:
        selection = stored.get("selection") or {}
        raw_strategy = selection.get("strategy")
        strategy = Strategy(raw_strategy) if raw_strategy in {s.value for s in Strategy} else None
        room_id = selection.get("room_id")
        if strategy is Strategy.ROOM and room_id in self.rooms and room_id not in self.unserved:
            self.selection = Selection.room(room_id)
        elif strategy in (Strategy.AVERAGE, Strategy.EXTREME):
            self.selection = Selection(strategy)
        targets = stored.get("targets") or {}
        self.targets = Setpoints(_float(targets.get("heat")), _float(targets.get("cool")))
        self.hold = bool(stored.get("hold", False))
        hold_until = stored.get("hold_until")
        self.hold_until = dt_util.parse_datetime(hold_until) if isinstance(hold_until, str) else None
        for room_id, (value, at) in (stored.get("filters") or {}).items():
            parsed = dt_util.parse_datetime(at) if isinstance(at, str) else None
            if room_id in self.rooms and _float(value) is not None and parsed is not None:
                self.filters[room_id] = OffsetFilter(float(value), parsed)
        signature = stored.get("block_signature")
        self.block_signature = signature if isinstance(signature, list) else None
        self.presets = presets_from_storage(stored.get("presets"), self.followable_rooms)
        self.schedule = schedule_from_storage(stored.get("schedule"), self.presets)
        active = stored.get("active_preset")
        self.active_preset = active if active in self.presets else None
        self.override = hold_from_dict(stored.get("override"), dt_util.parse_datetime)
        key = stored.get("block_key")
        self.block_key = key if isinstance(key, list) else None
        revision = stored.get("config_revision")
        self.config_revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
        self.baseline = self._restore_baseline(stored.get("baseline"))

    def _restore_baseline(self, data: Any) -> tuple[Setpoints, Selection, str | None] | None:
        if not isinstance(data, Mapping):
            return None
        targets = data.get("targets") or {}
        heat, cool = _float(targets.get("heat")), _float(targets.get("cool"))
        selection = parse_selection(data.get("selection"), self.followable_rooms) or self.default_selection
        preset = data.get("preset")
        if heat is None or cool is None:
            return None
        return Setpoints(heat, cool), selection, preset if preset in self.presets else None

    def _save(self) -> None:
        self._store.async_delay_save(self._serialize, 5)

    async def async_flush(self) -> None:
        """Write pending state now (unload/reload would otherwise drop a delayed save)."""
        await self._store.async_save(self._serialize())

    def _serialize(self) -> dict[str, Any]:
        return {
            "selection": {"strategy": self.selection.strategy.value, "room_id": self.selection.room_id},
            "targets": {"heat": self.targets.heat, "cool": self.targets.cool},
            "hold": self.hold,
            "hold_until": None if self.hold_until is None else self.hold_until.isoformat(),
            "filters": {
                room_id: [offset.value, offset.updated_at.isoformat()]
                for room_id, offset in self.filters.items()
                if offset.value is not None and offset.updated_at is not None
            },
            "block_signature": self.block_signature,
            "presets": [preset_to_dict(preset) for preset in self.presets.values()],
            "schedule": schedule_to_list(self.schedule),
            "active_preset": self.active_preset,
            "override": hold_to_dict(self.override),
            "baseline": None
            if self.baseline is None
            else {
                "targets": {"heat": self.baseline[0].heat, "cool": self.baseline[0].cool},
                "selection": selection_to_str(self.baseline[1]),
                "preset": self.baseline[2],
            },
            "block_key": self.block_key,
            "config_revision": self.config_revision,
        }
