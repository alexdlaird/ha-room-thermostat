"""Presets, the weekly schedule and holds: pure rules, no Home Assistant state.

A preset is a named pair of room targets plus which room(s) to follow. The schedule is a list of
(time -> preset) blocks per weekday; a block stays in effect until the next one, carrying over
midnight and the end of the week. A hold is a change made through Home Assistant that outranks the
schedule for a while: until the next block, for a number of minutes, or until someone resumes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
import re
from typing import Any, Final

from .control import Selection, Setpoints, Strategy, parse_selection

HOME: Final = "home"
AWAY: Final = "away"
SLEEP: Final = "sleep"
BUILT_IN_PRESETS: Final = (HOME, AWAY, SLEEP)
BUILT_IN_NAMES: Final = {HOME: "Home", AWAY: "Away", SLEEP: "Sleep"}

DAYS: Final = 7
MAX_BLOCKS_PER_DAY: Final = 12
MAX_PRESETS: Final = 20
MAX_NAME_LENGTH: Final = 40
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class HoldKind(StrEnum):
    """How long a change made through Home Assistant outranks the schedule."""

    NEXT_BLOCK = "next_block"
    UNTIL = "until"
    INDEFINITE = "indefinite"


@dataclass(frozen=True)
class Preset:
    """Room targets plus which room(s) to follow."""

    preset_id: str
    name: str
    heat: float
    cool: float
    selection: Selection

    @property
    def targets(self) -> Setpoints:
        return Setpoints(self.heat, self.cool)

    @property
    def built_in(self) -> bool:
        return self.preset_id in BUILT_IN_PRESETS


@dataclass(frozen=True)
class ScheduleBlock:
    """From `start` (local wall-clock time) on this day, follow `preset_id`."""

    start: time
    preset_id: str


@dataclass(frozen=True)
class Hold:
    """A change made through Home Assistant that outranks the schedule."""

    kind: HoldKind
    until: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return self.kind is HoldKind.UNTIL and self.until is not None and now >= self.until


#: Monday first, as `datetime.weekday()`.
type Schedule = tuple[tuple[ScheduleBlock, ...], ...]

EMPTY_SCHEDULE: Final[Schedule] = ((),) * DAYS


class PlanError(ValueError):
    """An invalid preset list, schedule or hold; `str()` is safe to show."""


def default_presets(home: Setpoints, selection: Selection, away: Setpoints, sleep: Setpoints) -> dict[str, Preset]:
    """The built-ins a new room thermostat starts with."""
    return {
        HOME: Preset(HOME, BUILT_IN_NAMES[HOME], _required(home.heat), _required(home.cool), selection),
        AWAY: Preset(
            AWAY, BUILT_IN_NAMES[AWAY], _required(away.heat), _required(away.cool), Selection(Strategy.AVERAGE)
        ),
        SLEEP: Preset(SLEEP, BUILT_IN_NAMES[SLEEP], _required(sleep.heat), _required(sleep.cool), selection),
    }


def _required(value: float | None) -> float:
    if value is None:
        raise PlanError("preset targets are required")
    return value


def active_block(schedule: Schedule, now: datetime) -> tuple[int, int, ScheduleBlock] | None:
    """The block in effect at local time `now` as (weekday, index, block); None for an empty schedule."""
    today = now.weekday()
    for days_back in range(DAYS + 1):
        weekday = (today - days_back) % DAYS
        blocks = schedule[weekday]
        candidates = [
            (index, block) for index, block in enumerate(blocks) if days_back > 0 or block.start <= now.time()
        ]
        if candidates:
            index, block = candidates[-1]
            return weekday, index, block
    return None


def next_block_start(schedule: Schedule, now: datetime) -> datetime | None:
    """When the next block starts after local time `now`; None for an empty schedule."""
    today = now.date()
    wall = now.time()
    for days_ahead in range(DAYS + 1):
        weekday = (now.weekday() + days_ahead) % DAYS
        for block in schedule[weekday]:
            if days_ahead == 0 and block.start <= wall:
                continue
            day = today + timedelta(days=days_ahead)
            return datetime.combine(day, block.start, tzinfo=now.tzinfo)
    return None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "preset"


def parse_presets(
    data: Any, rooms: Mapping[str, str], minimum_range: float, existing: Mapping[str, Preset]
) -> dict[str, Preset]:
    """A full preset list from a client. Built-ins must stay; new presets get ids from their names."""
    if not isinstance(data, list) or not data:
        raise PlanError("presets must be a non-empty list")
    if len(data) > MAX_PRESETS:
        raise PlanError(f"at most {MAX_PRESETS} presets")
    presets: dict[str, Preset] = {}
    names: set[str] = set()
    for item in data:
        preset = _parse_preset(item, rooms, minimum_range, existing, presets)
        folded = preset.name.casefold()
        if folded in names:
            raise PlanError(f"two presets are named {preset.name!r}")
        names.add(folded)
        presets[preset.preset_id] = preset
    missing = [BUILT_IN_NAMES[preset_id] for preset_id in BUILT_IN_PRESETS if preset_id not in presets]
    if missing:
        raise PlanError(f"built-in presets cannot be removed: {', '.join(missing)}")
    return presets


def _parse_preset(
    item: Any,
    rooms: Mapping[str, str],
    minimum_range: float,
    existing: Mapping[str, Preset],
    taken: Mapping[str, Preset],
) -> Preset:
    if not isinstance(item, Mapping):
        raise PlanError("each preset must be an object")
    name = str(item.get("name", "")).strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise PlanError(f"preset names must be 1-{MAX_NAME_LENGTH} characters")
    heat, cool = _number(item.get("heat"), "heat"), _number(item.get("cool"), "cool")
    if cool - heat < minimum_range:
        raise PlanError(f"{name}: heat and cool must be at least {minimum_range:g} apart")
    selection = parse_selection(item.get("room"), rooms)
    if selection is None:
        raise PlanError(f"{name}: unknown room {item.get('room')!r}")
    preset_id = item.get("id")
    if preset_id is not None and (not isinstance(preset_id, str) or preset_id not in existing):
        raise PlanError(f"{name}: unknown preset id {preset_id!r}")
    if preset_id is None:
        preset_id = _new_id(name, {*existing, *taken})
    return Preset(preset_id, name, heat, cool, selection)


def _new_id(name: str, taken: set[str]) -> str:
    base = candidate = slugify(name)
    suffix = 2
    while candidate in taken or candidate in BUILT_IN_PRESETS:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanError(f"{label} must be a number")
    return float(value)


def parse_schedule(data: Any, presets: Mapping[str, Preset]) -> Schedule:
    """Seven day lists (Monday first) of {time: "HH:MM", preset: id}; sorted, one block per time."""
    if not isinstance(data, list) or len(data) != DAYS:
        raise PlanError(f"the schedule must have {DAYS} days, Monday first")
    days: list[tuple[ScheduleBlock, ...]] = []
    for day in data:
        if not isinstance(day, list) or len(day) > MAX_BLOCKS_PER_DAY:
            raise PlanError(f"each day must be a list of at most {MAX_BLOCKS_PER_DAY} blocks")
        blocks = sorted((_parse_block(item, presets) for item in day), key=lambda block: block.start)
        starts = [block.start for block in blocks]
        if len(set(starts)) != len(starts):
            raise PlanError("two blocks on the same day start at the same time")
        days.append(tuple(blocks))
    return tuple(days)


def _parse_block(item: Any, presets: Mapping[str, Preset]) -> ScheduleBlock:
    if not isinstance(item, Mapping):
        raise PlanError("each block must be an object")
    match = _TIME.match(str(item.get("time", "")))
    if match is None:
        raise PlanError(f"block times are HH:MM (24-hour), not {item.get('time')!r}")
    preset_id = item.get("preset")
    if preset_id not in presets:
        raise PlanError(f"unknown preset {preset_id!r}")
    return ScheduleBlock(time(int(match[1]), int(match[2])), str(preset_id))


def missing_presets(presets: Mapping[str, Preset], schedule: Schedule) -> Sequence[str]:
    """Preset ids the schedule uses that `presets` does not have (a preset in use cannot be deleted)."""
    return sorted({block.preset_id for day in schedule for block in day if block.preset_id not in presets})


def parse_hold(data: Any, now: datetime) -> Hold:
    """{kind: next_block} | {kind: minutes, minutes: N} | {kind: indefinite}; missing = next_block."""
    if data is None:
        return Hold(HoldKind.NEXT_BLOCK)
    if not isinstance(data, Mapping):
        raise PlanError("hold must be an object")
    kind = data.get("kind", HoldKind.NEXT_BLOCK)
    if kind == HoldKind.NEXT_BLOCK:
        return Hold(HoldKind.NEXT_BLOCK)
    if kind == HoldKind.INDEFINITE:
        return Hold(HoldKind.INDEFINITE)
    if kind == "minutes":
        minutes = data.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or not 0 < minutes <= 7 * 24 * 60:
            raise PlanError("hold minutes must be between 1 and 10080")
        return Hold(HoldKind.UNTIL, now + timedelta(minutes=float(minutes)))
    raise PlanError(f"unknown hold kind {kind!r}")


# ------------------------------------------------------------------ storage


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    return {
        "id": preset.preset_id,
        "name": preset.name,
        "heat": preset.heat,
        "cool": preset.cool,
        "room": selection_to_str(preset.selection),
    }


def selection_to_str(selection: Selection) -> str:
    return selection.room_id if selection.strategy is Strategy.ROOM and selection.room_id else selection.strategy.value


def schedule_to_list(schedule: Schedule) -> list[list[dict[str, str]]]:
    return [[{"time": block.start.strftime("%H:%M"), "preset": block.preset_id} for block in day] for day in schedule]


def hold_to_dict(hold: Hold | None) -> dict[str, Any] | None:
    if hold is None:
        return None
    return {"kind": hold.kind.value, "until": None if hold.until is None else hold.until.isoformat()}


def hold_from_dict(data: Any, parse_datetime: Any) -> Hold | None:
    if not isinstance(data, Mapping) or data.get("kind") not in {kind.value for kind in HoldKind}:
        return None
    kind = HoldKind(data["kind"])
    until = parse_datetime(data["until"]) if isinstance(data.get("until"), str) else None
    if kind is HoldKind.UNTIL and until is None:
        return None
    return Hold(kind, until)


def presets_from_storage(data: Any, rooms: Mapping[str, str]) -> dict[str, Preset]:
    """Stored presets, skipping any that no longer make sense (e.g. a removed room)."""
    presets: dict[str, Preset] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        selection = parse_selection(item.get("room"), rooms)
        try:
            heat, cool = _number(item.get("heat"), "heat"), _number(item.get("cool"), "cool")
        except PlanError:
            continue
        if selection is None:
            continue
        presets[item["id"]] = Preset(item["id"], str(item.get("name") or item["id"]), heat, cool, selection)
    return presets


def schedule_from_storage(data: Any, presets: Mapping[str, Preset]) -> Schedule:
    """The stored schedule, dropping blocks whose preset is gone; empty if unreadable."""
    if not isinstance(data, list) or len(data) != DAYS:
        return EMPTY_SCHEDULE
    days: list[tuple[ScheduleBlock, ...]] = []
    for day in data:
        blocks: list[ScheduleBlock] = []
        for item in day if isinstance(day, list) else []:
            try:
                blocks.append(_parse_block(item, presets))
            except PlanError:
                continue
        days.append(tuple(sorted(blocks, key=lambda block: block.start)))
    return tuple(days)
