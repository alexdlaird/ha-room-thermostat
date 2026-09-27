from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.room_thermostat.control import Selection, Setpoints, Strategy
from custom_components.room_thermostat.planner import (
    AWAY,
    EMPTY_SCHEDULE,
    HOME,
    SLEEP,
    Hold,
    HoldKind,
    PlanError,
    Preset,
    ScheduleBlock,
    active_block,
    default_presets,
    hold_from_dict,
    hold_to_dict,
    missing_presets,
    next_block_start,
    parse_hold,
    parse_presets,
    parse_schedule,
    preset_to_dict,
    presets_from_storage,
    schedule_from_storage,
    schedule_to_list,
    slugify,
)

ROOMS = {"living": "Living", "bed": "Bed"}
PRESETS = {
    HOME: Preset(HOME, "Home", 68, 74, Selection.room("living")),
    AWAY: Preset(AWAY, "Away", 62, 80, Selection(Strategy.AVERAGE)),
    SLEEP: Preset(SLEEP, "Sleep", 66, 72, Selection.room("bed")),
}
#: Monday 2026-09-28.
MONDAY = datetime(2026, 9, 28, tzinfo=UTC)


def _week(**days: list[tuple[str, str]]) -> list[list[dict[str, str]]]:
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return [[{"time": at, "preset": preset} for at, preset in days.get(name, [])] for name in names]


def _preset(name: str, heat: float = 68, cool: float = 74, room: str = "living", **extra: object) -> dict[str, object]:
    return {"name": name, "heat": heat, "cool": cool, "room": room, **extra}


def _all_built_ins() -> list[dict[str, object]]:
    return [_preset("Home", id=HOME), _preset("Away", 62, 80, "average", id=AWAY), _preset("Sleep", id=SLEEP)]


def test_defaults_make_home_from_todays_targets() -> None:
    # WHEN
    presets = default_presets(Setpoints(67, 73), Selection.room("bed"), Setpoints(62, 80), Setpoints(66, 76))

    # THEN
    assert list(presets) == [HOME, AWAY, SLEEP]
    assert (presets[HOME].heat, presets[HOME].cool, presets[HOME].selection) == (67, 73, Selection.room("bed"))
    assert presets[AWAY].selection == Selection(Strategy.AVERAGE)
    assert all(preset.built_in for preset in presets.values())


def test_defaults_need_targets() -> None:
    # THEN
    with pytest.raises(PlanError):
        default_presets(Setpoints(None, 73), Selection(Strategy.AVERAGE), Setpoints(62, 80), Setpoints(66, 76))


def test_a_block_carries_over_midnight_and_the_week() -> None:
    # GIVEN
    schedule = parse_schedule(_week(mon=[("07:00", HOME), ("22:00", SLEEP)], sun=[("09:00", AWAY)]), PRESETS)

    # THEN
    assert active_block(schedule, MONDAY.replace(hour=6)) == (6, 0, ScheduleBlock(time(9), AWAY))
    assert active_block(schedule, MONDAY.replace(hour=7)) == (0, 0, ScheduleBlock(time(7), HOME))
    assert active_block(schedule, MONDAY.replace(hour=23)) == (0, 1, ScheduleBlock(time(22), SLEEP))
    assert active_block(schedule, MONDAY + timedelta(days=1, hours=3)) == (0, 1, ScheduleBlock(time(22), SLEEP))
    assert active_block(EMPTY_SCHEDULE, MONDAY) is None


def test_the_next_block_start_looks_ahead_through_the_week() -> None:
    # GIVEN
    schedule = parse_schedule(_week(mon=[("07:00", HOME)], wed=[("08:30", AWAY)]), PRESETS)

    # THEN
    assert next_block_start(schedule, MONDAY.replace(hour=6)) == MONDAY.replace(hour=7)
    assert next_block_start(schedule, MONDAY.replace(hour=7)) == MONDAY + timedelta(days=2, hours=8, minutes=30)
    assert next_block_start(schedule, MONDAY + timedelta(days=3)) == MONDAY + timedelta(days=7, hours=7)
    assert next_block_start(EMPTY_SCHEDULE, MONDAY) is None


def test_schedule_days_are_sorted_and_validated() -> None:
    # WHEN
    schedule = parse_schedule(_week(tue=[("22:00", SLEEP), ("06:30", HOME)]), PRESETS)

    # THEN
    assert schedule[1] == (ScheduleBlock(time(6, 30), HOME), ScheduleBlock(time(22), SLEEP))
    assert schedule_to_list(schedule)[1] == [{"time": "06:30", "preset": HOME}, {"time": "22:00", "preset": SLEEP}]


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ([[]] * 6, "7 days"),
        ("weekly", "7 days"),
        (_week(mon=[("7:00", HOME)]), "HH:MM"),
        (_week(mon=[("24:00", HOME)]), "HH:MM"),
        (_week(mon=[("07:00", "party")]), "unknown preset"),
        (_week(mon=[("07:00", HOME), ("07:00", AWAY)]), "same time"),
        ([["07:00"], [], [], [], [], [], []], "object"),
        ([[{"time": f"{hour:02}:00", "preset": HOME} for hour in range(13)], [], [], [], [], [], []], "at most"),
    ],
)
def test_invalid_schedules_are_rejected(schedule: object, message: str) -> None:
    # THEN
    with pytest.raises(PlanError, match=message):
        parse_schedule(schedule, PRESETS)


def test_new_presets_get_ids_from_their_names() -> None:
    # WHEN
    presets = parse_presets([*_all_built_ins(), _preset("Movie night"), _preset("Home office")], ROOMS, 1.0, PRESETS)

    # THEN
    assert list(presets) == [HOME, AWAY, SLEEP, "movie_night", "home_office"]
    assert presets["movie_night"].selection == Selection.room("living")


def test_a_new_preset_id_never_collides() -> None:
    # WHEN
    presets = parse_presets([*_all_built_ins(), _preset("Away!"), _preset("Movie")], ROOMS, 1.0, PRESETS)

    # THEN
    assert "away_2" in presets
    assert slugify("!!!") == "preset"


@pytest.mark.parametrize(
    ("presets", "message"),
    [
        ([], "non-empty"),
        ({"name": "Home"}, "non-empty"),
        ([_preset("Home", id=HOME)], "cannot be removed"),
        ([*_all_built_ins(), _preset("home")], "two presets"),
        ([*_all_built_ins(), _preset("")], "1-40"),
        ([*_all_built_ins(), _preset("x" * 41)], "1-40"),
        ([*_all_built_ins(), _preset("Tight", 70, 70.5)], "apart"),
        ([*_all_built_ins(), _preset("Nowhere", room="attic")], "unknown room"),
        ([*_all_built_ins(), _preset("Ghost", id="ghost")], "unknown preset id"),
        ([*_all_built_ins(), _preset("Words", heat="warm")], "number"),
        ([*_all_built_ins(), "Home"], "object"),
        ([*_all_built_ins(), *[_preset(f"P{n}") for n in range(18)]], "at most"),
    ],
)
def test_invalid_preset_lists_are_rejected(presets: object, message: str) -> None:
    # THEN
    with pytest.raises(PlanError, match=message):
        parse_presets(presets, ROOMS, 1.0, PRESETS)


def test_a_preset_the_schedule_uses_is_reported_missing() -> None:
    # GIVEN
    schedule = parse_schedule(_week(fri=[("18:00", SLEEP)]), PRESETS)

    # THEN
    assert missing_presets({HOME: PRESETS[HOME]}, schedule) == [SLEEP]
    assert missing_presets(PRESETS, schedule) == []


def test_holds_parse_and_expire() -> None:
    # WHEN
    timed = parse_hold({"kind": "minutes", "minutes": 120}, MONDAY)

    # THEN
    assert timed == Hold(HoldKind.UNTIL, MONDAY + timedelta(hours=2))
    assert not timed.expired(MONDAY + timedelta(minutes=119))
    assert timed.expired(MONDAY + timedelta(hours=2))
    assert parse_hold(None, MONDAY) == Hold(HoldKind.NEXT_BLOCK)
    assert parse_hold({}, MONDAY) == Hold(HoldKind.NEXT_BLOCK)
    assert parse_hold({"kind": "indefinite"}, MONDAY) == Hold(HoldKind.INDEFINITE)
    assert not Hold(HoldKind.INDEFINITE).expired(MONDAY)


@pytest.mark.parametrize(
    "hold",
    [
        "2h",
        {"kind": "minutes"},
        {"kind": "minutes", "minutes": 0},
        {"kind": "minutes", "minutes": True},
        {"kind": "forever"},
    ],
)
def test_invalid_holds_are_rejected(hold: object) -> None:
    # THEN
    with pytest.raises(PlanError):
        parse_hold(hold, MONDAY)


def test_holds_round_trip_through_storage() -> None:
    # GIVEN
    timed = Hold(HoldKind.UNTIL, MONDAY)

    # THEN
    assert hold_from_dict(hold_to_dict(timed), datetime.fromisoformat) == timed
    assert hold_from_dict(hold_to_dict(Hold(HoldKind.INDEFINITE)), datetime.fromisoformat) == Hold(HoldKind.INDEFINITE)
    assert hold_to_dict(None) is None
    assert hold_from_dict({"kind": "until"}, datetime.fromisoformat) is None
    assert hold_from_dict({"kind": "later"}, datetime.fromisoformat) is None
    assert hold_from_dict("next_block", datetime.fromisoformat) is None


def test_presets_and_schedule_round_trip_through_storage_skipping_what_no_longer_fits() -> None:
    # GIVEN
    stored = [preset_to_dict(preset) for preset in PRESETS.values()]
    stored += [{"id": "attic", "name": "Attic", "heat": 60, "cool": 80, "room": "attic"}, {"name": "No id"}, "junk"]
    stored += [{"id": "bad", "name": "Bad", "heat": "cold", "cool": 80, "room": "living"}]

    # WHEN
    presets = presets_from_storage(stored, ROOMS)
    schedule = schedule_from_storage(_week(mon=[("07:00", HOME), ("08:00", "attic"), ("25:00", HOME)]), presets)

    # THEN
    assert presets == PRESETS
    assert schedule[0] == (ScheduleBlock(time(7), HOME),)
    assert schedule_from_storage("weekly", presets) == EMPTY_SCHEDULE
    assert presets_from_storage(None, ROOMS) == {}
