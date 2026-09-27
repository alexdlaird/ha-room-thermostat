"""Room control rules, free of Home Assistant so every one of them is unit-testable.

The controller never regulates the equipment itself. It measures how far the thermostat's own
reading is from the room that matters (the offset), shifts the thermostat's setpoints by that
offset, and lets the thermostat keep doing the regulating. All temperatures here share one unit
(Home Assistant's configured one); offsets and thresholds are differences in that unit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
import math
from typing import Any, Final

#: Setpoint differences at or below this are the same setpoint (unit round-trips, API rounding).
SETPOINT_TOLERANCE: Final = 0.25


class LiveMode(StrEnum):
    """Which of the thermostat's setpoints are in effect."""

    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"
    NONE = "none"


class Strategy(StrEnum):
    """How the room temperature is chosen from the configured rooms."""

    ROOM = "room"
    AVERAGE = "average"
    EXTREME = "extreme"


class ControlState(StrEnum):
    """What the controller is doing right now."""

    CONTROLLING = "controlling"
    FALLBACK_REFERENCE = "fallback_reference"
    FALLBACK_THERMOSTAT = "fallback_thermostat"
    MANUAL_HOLD = "manual_hold"
    IDLE = "idle"
    UNDERLYING_UNAVAILABLE = "underlying_unavailable"


class ManualChangePolicy(StrEnum):
    """What to do when someone changes the thermostat outside Home Assistant."""

    HOLD = "hold"
    ADOPT = "adopt"


@dataclass(frozen=True)
class Selection:
    """The active room, or a strategy across all rooms."""

    strategy: Strategy
    room_id: str | None = None

    @classmethod
    def room(cls, room_id: str) -> Selection:
        return cls(Strategy.ROOM, room_id)


@dataclass(frozen=True)
class Setpoints:
    """Heat and cool setpoints; None where a side is not known or not live."""

    heat: float | None = None
    cool: float | None = None

    def live(self, mode: LiveMode) -> Setpoints:
        """Only the sides the mode uses."""
        return Setpoints(
            self.heat if mode in (LiveMode.HEAT, LiveMode.HEAT_COOL) else None,
            self.cool if mode in (LiveMode.COOL, LiveMode.HEAT_COOL) else None,
        )


@dataclass(frozen=True)
class Settings:
    """Tunables, in the configured temperature unit."""

    stale_after: timedelta
    max_offset: float
    deadband: float
    min_write_interval: timedelta
    smoothing: timedelta
    setpoint_step: float
    minimum_range: float


@dataclass
class OffsetFilter:
    """Exponential moving average of thermostat-minus-room, weighted by elapsed time."""

    value: float | None = None
    updated_at: datetime | None = None

    def update(self, raw: float, at: datetime, smoothing: timedelta) -> None:
        if self.value is None or self.updated_at is None or smoothing.total_seconds() <= 0:
            self.value, self.updated_at = raw, at
            return
        elapsed = max((at - self.updated_at).total_seconds(), 0.0)
        weight = 1 - math.exp(-elapsed / smoothing.total_seconds())
        self.value += weight * (raw - self.value)
        self.updated_at = at


@dataclass(frozen=True)
class RoomReading:
    """A room temperature and when its sensor last reported."""

    temperature: float
    reported_at: datetime


@dataclass(frozen=True)
class RoomChoice:
    """The rooms that currently define the room temperature, and why."""

    room_ids: tuple[str, ...]
    state: ControlState


@dataclass(frozen=True)
class Block:
    """Values carried by a schedule block; None where the block does not set them."""

    selection: Selection | None = None
    heat: float | None = None
    cool: float | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


def fresh_readings(
    readings: Mapping[str, RoomReading | None], now: datetime, stale_after: timedelta
) -> dict[str, float]:
    """Temperatures of the rooms whose sensor reported within `stale_after`."""
    return {
        room_id: reading.temperature
        for room_id, reading in readings.items()
        if reading is not None and now - reading.reported_at <= stale_after
    }


def choose_rooms(
    selection: Selection,
    fresh: Mapping[str, float],
    reference_id: str | None,
    mode: LiveMode,
    targets: Setpoints,
) -> RoomChoice:
    """Apply the selection to the fresh rooms, falling back to the reference room, then to none."""
    chosen = _select(selection, fresh, mode, targets)
    if chosen:
        return RoomChoice(chosen, ControlState.CONTROLLING)
    if reference_id is not None and reference_id in fresh:
        return RoomChoice((reference_id,), ControlState.FALLBACK_REFERENCE)
    return RoomChoice((), ControlState.FALLBACK_THERMOSTAT)


def _select(selection: Selection, fresh: Mapping[str, float], mode: LiveMode, targets: Setpoints) -> tuple[str, ...]:
    if selection.strategy is Strategy.ROOM:
        room_id = selection.room_id
        return (room_id,) if room_id is not None and room_id in fresh else ()
    if not fresh:
        return ()
    if selection.strategy is Strategy.AVERAGE:
        return tuple(sorted(fresh))
    return _most_off_target(fresh, mode, targets)


def _most_off_target(fresh: Mapping[str, float], mode: LiveMode, targets: Setpoints) -> tuple[str, ...]:
    """Coldest room when heating, hottest when cooling; in heat_cool the room furthest outside the range."""
    if mode is LiveMode.HEAT:
        return (min(fresh, key=lambda room_id: (fresh[room_id], room_id)),)
    if mode is LiveMode.COOL:
        return (max(fresh, key=lambda room_id: (fresh[room_id], room_id)),)
    if mode is LiveMode.HEAT_COOL and targets.heat is not None and targets.cool is not None:
        low, high = targets.heat, targets.cool
        deviation = {room_id: max(low - temp, temp - high, 0.0) for room_id, temp in fresh.items()}
        worst = max(deviation.values())
        if worst > 0:
            return (max(deviation, key=lambda room_id: (deviation[room_id], room_id)),)
    return tuple(sorted(fresh))


def mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def effective_offset(
    choice: RoomChoice, filters: Mapping[str, OffsetFilter], reference_id: str | None, max_offset: float
) -> float:
    """Offset to apply: the chosen rooms' smoothed offsets, else the reference room's last known one, else 0."""
    if choice.room_ids:
        offset = mean(value for room_id in choice.room_ids if (value := _filter_value(filters, room_id)) is not None)
    else:
        offset = _filter_value(filters, reference_id)
    return clamp(offset or 0.0, -max_offset, max_offset)


def _filter_value(filters: Mapping[str, OffsetFilter], room_id: str | None) -> float | None:
    if room_id is None or room_id not in filters:
        return None
    return filters[room_id].value


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 4)


def commanded_setpoints(
    mode: LiveMode,
    targets: Setpoints,
    offset: float,
    *,
    minimum: float,
    maximum: float,
    settings: Settings,
) -> Setpoints:
    """Room targets shifted by the offset, stepped and clamped, for the sides the mode uses."""

    def shift(target: float | None) -> float | None:
        if target is None:
            return None
        return clamp(round_to_step(target + offset, settings.setpoint_step), minimum, maximum)

    live = targets.live(mode)
    heat, cool = shift(live.heat), shift(live.cool)
    if heat is not None and cool is not None and cool - heat < settings.minimum_range:
        # Only reachable when clamping squeezes the range against a thermostat limit.
        cool = min(heat + settings.minimum_range, maximum)
        heat = max(cool - settings.minimum_range, minimum)
    return Setpoints(heat, cool)


def needs_write(commanded: Setpoints, current: Setpoints, deadband: float) -> bool:
    """True when any commanded side differs from the thermostat by at least the deadband."""
    for want, have in ((commanded.heat, current.heat), (commanded.cool, current.cool)):
        if want is None:
            continue
        if have is None or abs(want - have) >= max(deadband, SETPOINT_TOLERANCE / 2):
            return True
    return False


def write_allowed(now: datetime, last_write_at: datetime | None, min_interval: timedelta, *, urgent: bool) -> bool:
    return urgent or last_write_at is None or now - last_write_at >= min_interval


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= SETPOINT_TOLERANCE


def is_external_change(previous: Setpoints | None, current: Setpoints, last_commanded: Setpoints | None) -> bool:
    """True when the thermostat's setpoints moved and the new values are not what we last asked for."""
    if previous is None:
        return False
    changed = [
        side
        for side, before, after in (("heat", previous.heat, current.heat), ("cool", previous.cool, current.cool))
        if not _same(before, after)
    ]
    if not changed:
        return False
    commanded_sides = [
        (want, getattr(current, side))
        for side in ("heat", "cool")
        if (want := getattr(last_commanded, side, None)) is not None
    ]
    # Our write landing moves the sides we commanded (and may push an inert side along with it).
    return not (commanded_sides and all(_same(want, have) for want, have in commanded_sides))


def adopted_targets(mode: LiveMode, current: Setpoints, offset: float, targets: Setpoints) -> Setpoints:
    """Room targets that keep the thermostat exactly where someone just put it."""
    live = current.live(mode)
    return replace(
        targets,
        heat=targets.heat if live.heat is None else live.heat - offset,
        cool=targets.cool if live.cool is None else live.cool - offset,
    )


def room_error(mode: LiveMode, temperature: float | None, targets: Setpoints) -> float | None:
    """Room temperature minus the target it should hold (0 inside a heat_cool range)."""
    if temperature is None:
        return None
    if mode is LiveMode.HEAT and targets.heat is not None:
        return temperature - targets.heat
    if mode is LiveMode.COOL and targets.cool is not None:
        return temperature - targets.cool
    if mode is LiveMode.HEAT_COOL and targets.heat is not None and targets.cool is not None:
        if temperature < targets.heat:
            return temperature - targets.heat
        if temperature > targets.cool:
            return temperature - targets.cool
        return 0.0
    return None


def parse_selection(value: Any, rooms: Mapping[str, str]) -> Selection | None:
    """A room id, a room name (any case), `average`, or `extreme`; None if it matches nothing."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    folded = text.casefold()
    if folded == Strategy.AVERAGE:
        return Selection(Strategy.AVERAGE)
    if folded == Strategy.EXTREME:
        return Selection(Strategy.EXTREME)
    for room_id, name in rooms.items():
        if folded in (room_id.casefold(), name.casefold()):
            return Selection.room(room_id)
    return None


def parse_block(data: Mapping[str, Any], rooms: Mapping[str, str]) -> Block:
    """Read `room`, `heat` and `cool` from a schedule block's data, collecting what is invalid."""
    errors: list[str] = []
    selection = None
    if "room" in data:
        selection = parse_selection(data["room"], rooms)
        if selection is None:
            errors.append(f"unknown room {data['room']!r}")
    heat = _number(data, "heat", errors)
    cool = _number(data, "cool", errors)
    return Block(selection, heat, cool, tuple(errors))


def _number(data: Mapping[str, Any], key: str, errors: list[str]) -> float | None:
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, bool):
        errors.append(f"{key} is not a number")
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        errors.append(f"{key} is not a number")
        return None
    if not math.isfinite(number):
        errors.append(f"{key} is not a number")
        return None
    return number
