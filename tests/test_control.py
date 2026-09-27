from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math

import pytest

from custom_components.room_thermostat.control import (
    Block,
    ControlState,
    LiveMode,
    OffsetFilter,
    RoomChoice,
    RoomReading,
    Selection,
    Setpoints,
    Settings,
    Strategy,
    adopted_targets,
    choose_rooms,
    clamp,
    commanded_setpoints,
    effective_offset,
    fresh_readings,
    is_external_change,
    mean,
    needs_write,
    parse_block,
    parse_selection,
    room_error,
    round_to_step,
    write_allowed,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
SETTINGS = Settings(
    stale_after=timedelta(minutes=10),
    max_offset=6.0,
    deadband=0.5,
    min_write_interval=timedelta(minutes=15),
    smoothing=timedelta(minutes=15),
    setpoint_step=0.5,
    minimum_range=5.0,
)
ROOMS = {"living": "Living", "office": "Office", "bed": "Bed"}


def test_offset_filter_takes_the_first_sample_as_is() -> None:
    # GIVEN
    offset = OffsetFilter()

    # WHEN
    offset.update(-3.0, NOW, timedelta(minutes=15))

    # THEN
    assert offset.value == -3.0
    assert offset.updated_at == NOW


def test_offset_filter_moves_by_the_time_weighted_fraction() -> None:
    # GIVEN
    offset = OffsetFilter(0.0, NOW)

    # WHEN
    offset.update(10.0, NOW + timedelta(minutes=15), timedelta(minutes=15))

    # THEN
    assert offset.value == pytest.approx(10.0 * (1 - math.exp(-1)))


def test_offset_filter_ignores_a_sample_with_no_elapsed_time() -> None:
    # GIVEN
    offset = OffsetFilter(1.0, NOW)

    # WHEN
    offset.update(9.0, NOW - timedelta(minutes=1), timedelta(minutes=15))

    # THEN
    assert offset.value == 1.0


def test_offset_filter_without_smoothing_follows_the_raw_value() -> None:
    # GIVEN
    offset = OffsetFilter(1.0, NOW)

    # WHEN
    offset.update(4.0, NOW + timedelta(minutes=1), timedelta(0))

    # THEN
    assert offset.value == 4.0


def test_fresh_readings_drop_stale_and_missing_rooms() -> None:
    # GIVEN
    readings = {
        "living": RoomReading(66.0, NOW - timedelta(minutes=10)),
        "office": RoomReading(64.0, NOW - timedelta(minutes=11)),
        "bed": None,
    }

    # WHEN
    fresh = fresh_readings(readings, NOW, timedelta(minutes=10))

    # THEN
    assert fresh == {"living": 66.0}


def test_a_fresh_selected_room_is_controlled() -> None:
    # WHEN
    choice = choose_rooms(
        Selection.room("office"), {"office": 64.0, "living": 66.0}, "living", LiveMode.HEAT, Setpoints()
    )

    # THEN
    assert choice == RoomChoice(("office",), ControlState.CONTROLLING)


def test_a_stale_selected_room_falls_back_to_the_reference_room() -> None:
    # WHEN
    choice = choose_rooms(Selection.room("office"), {"living": 66.0}, "living", LiveMode.HEAT, Setpoints())

    # THEN
    assert choice == RoomChoice(("living",), ControlState.FALLBACK_REFERENCE)


@pytest.mark.parametrize("reference", [None, "living"])
def test_no_fresh_room_falls_back_to_the_thermostat(reference: str | None) -> None:
    # WHEN
    choice = choose_rooms(Selection.room("office"), {}, reference, LiveMode.HEAT, Setpoints())

    # THEN
    assert choice == RoomChoice((), ControlState.FALLBACK_THERMOSTAT)


def test_average_uses_every_fresh_room() -> None:
    # WHEN
    choice = choose_rooms(
        Selection(Strategy.AVERAGE), {"office": 64.0, "living": 66.0}, None, LiveMode.HEAT, Setpoints()
    )

    # THEN
    assert choice.room_ids == ("living", "office")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(LiveMode.HEAT, ("office",)), (LiveMode.COOL, ("bed",)), (LiveMode.NONE, ("bed", "living", "office"))],
)
def test_extreme_picks_the_room_that_needs_conditioning_most(mode: LiveMode, expected: tuple[str, ...]) -> None:
    # GIVEN
    fresh = {"living": 66.0, "office": 63.0, "bed": 70.0}

    # WHEN
    choice = choose_rooms(Selection(Strategy.EXTREME), fresh, None, mode, Setpoints(65.0, 72.0))

    # THEN
    assert choice.room_ids == expected


@pytest.mark.parametrize(
    ("fresh", "expected"),
    [
        ({"living": 66.0, "office": 62.0, "bed": 74.0}, ("office",)),
        ({"living": 66.0, "office": 64.0, "bed": 76.0}, ("bed",)),
        ({"living": 66.0, "office": 68.0}, ("living", "office")),
    ],
)
def test_extreme_in_heat_cool_picks_the_room_furthest_outside_the_range(
    fresh: dict[str, float], expected: tuple[str, ...]
) -> None:
    # WHEN
    choice = choose_rooms(Selection(Strategy.EXTREME), fresh, None, LiveMode.HEAT_COOL, Setpoints(65.0, 72.0))

    # THEN
    assert choice.room_ids == expected


def test_extreme_with_no_fresh_rooms_falls_back() -> None:
    # WHEN
    choice = choose_rooms(Selection(Strategy.EXTREME), {}, None, LiveMode.HEAT, Setpoints())

    # THEN
    assert choice.state is ControlState.FALLBACK_THERMOSTAT


def test_effective_offset_averages_the_chosen_rooms() -> None:
    # GIVEN
    filters = {"living": OffsetFilter(-3.0, NOW), "office": OffsetFilter(-1.0, NOW), "bed": OffsetFilter(5.0, NOW)}

    # WHEN
    offset = effective_offset(RoomChoice(("living", "office"), ControlState.CONTROLLING), filters, "living", 6.0)

    # THEN
    assert offset == -2.0


def test_effective_offset_skips_rooms_without_a_learned_offset() -> None:
    # GIVEN
    filters = {"living": OffsetFilter(-3.0, NOW), "office": OffsetFilter()}

    # WHEN
    offset = effective_offset(RoomChoice(("living", "office"), ControlState.CONTROLLING), filters, None, 6.0)

    # THEN
    assert offset == -3.0


def test_effective_offset_without_rooms_uses_the_reference_rooms_last_offset() -> None:
    # GIVEN
    filters = {"living": OffsetFilter(-2.5, NOW)}

    # WHEN
    offset = effective_offset(RoomChoice((), ControlState.FALLBACK_THERMOSTAT), filters, "living", 6.0)

    # THEN
    assert offset == -2.5


@pytest.mark.parametrize("reference", [None, "missing"])
def test_effective_offset_with_nothing_learned_is_zero(reference: str | None) -> None:
    # WHEN
    offset = effective_offset(RoomChoice((), ControlState.FALLBACK_THERMOSTAT), {}, reference, 6.0)

    # THEN
    assert offset == 0.0


def test_effective_offset_is_capped() -> None:
    # GIVEN
    filters = {"living": OffsetFilter(-9.0, NOW)}

    # WHEN
    offset = effective_offset(RoomChoice(("living",), ControlState.CONTROLLING), filters, None, 6.0)

    # THEN
    assert offset == -6.0


def test_mean_of_nothing_is_none() -> None:
    # THEN
    assert mean([]) is None


@pytest.mark.parametrize(
    ("value", "step", "expected"), [(66.26, 0.5, 66.5), (66.24, 0.5, 66.0), (19.17, 0.1, 19.2), (7.3, 0, 7.3)]
)
def test_round_to_step(value: float, step: float, expected: float) -> None:
    # THEN
    assert round_to_step(value, step) == expected


def test_clamp() -> None:
    # THEN
    assert (clamp(1, 2, 3), clamp(4, 2, 3), clamp(2.5, 2, 3)) == (2, 3, 2.5)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (LiveMode.HEAT, Setpoints(65.0, None)),
        (LiveMode.COOL, Setpoints(None, 71.0)),
        (LiveMode.HEAT_COOL, Setpoints(65.0, 71.0)),
        (LiveMode.NONE, Setpoints(None, None)),
    ],
)
def test_commanded_setpoints_shift_the_live_sides(mode: LiveMode, expected: Setpoints) -> None:
    # WHEN
    commanded = commanded_setpoints(mode, Setpoints(68.0, 74.0), -3.0, minimum=50.0, maximum=90.0, settings=SETTINGS)

    # THEN
    assert commanded == expected


def test_commanded_setpoints_are_stepped_and_clamped() -> None:
    # WHEN
    commanded = commanded_setpoints(
        LiveMode.HEAT_COOL, Setpoints(52.0, 88.4), -2.7, minimum=50.0, maximum=85.0, settings=SETTINGS
    )

    # THEN
    assert commanded == Setpoints(50.0, 85.0)


def test_commanded_setpoints_keep_the_minimum_range_against_a_limit() -> None:
    # WHEN
    commanded = commanded_setpoints(
        LiveMode.HEAT_COOL, Setpoints(84.0, 89.0), 3.0, minimum=50.0, maximum=90.0, settings=SETTINGS
    )

    # THEN
    assert commanded == Setpoints(85.0, 90.0)


@pytest.mark.parametrize(
    ("commanded", "current", "expected"),
    [
        (Setpoints(65.0, None), Setpoints(65.4, None), False),
        (Setpoints(65.0, None), Setpoints(65.5, None), True),
        (Setpoints(65.0, None), Setpoints(None, None), True),
        (Setpoints(None, 71.0), Setpoints(65.0, 71.2), False),
        (Setpoints(65.0, 71.0), Setpoints(65.0, 70.0), True),
        (Setpoints(None, None), Setpoints(55.0, 69.0), False),
    ],
)
def test_needs_write(commanded: Setpoints, current: Setpoints, expected: bool) -> None:
    # THEN
    assert needs_write(commanded, current, 0.5) is expected


def test_needs_write_with_no_deadband_still_ignores_rounding_noise() -> None:
    # THEN
    assert needs_write(Setpoints(65.0), Setpoints(65.1), 0.0) is False


@pytest.mark.parametrize(
    ("last", "urgent", "expected"),
    [
        (None, False, True),
        (NOW - timedelta(minutes=14), False, False),
        (NOW - timedelta(minutes=15), False, True),
        (NOW, True, True),
    ],
)
def test_write_allowed(last: datetime | None, urgent: bool, expected: bool) -> None:
    # THEN
    assert write_allowed(NOW, last, timedelta(minutes=15), urgent=urgent) is expected


@pytest.mark.parametrize(
    ("previous", "current", "commanded", "expected"),
    [
        (None, Setpoints(65.0), None, False),
        (Setpoints(65.0), Setpoints(65.1), None, False),
        (Setpoints(65.0), Setpoints(67.0), None, True),
        (Setpoints(65.0), Setpoints(67.0), Setpoints(67.0), False),
        (Setpoints(65.0), Setpoints(68.0), Setpoints(67.0), True),
        (Setpoints(65.0, 70.0), Setpoints(67.0, 72.0), Setpoints(67.0, None), False),
        (Setpoints(65.0, 70.0), Setpoints(65.0, 73.0), Setpoints(65.0, 70.0), True),
        (Setpoints(65.0), Setpoints(67.0), Setpoints(None, None), True),
    ],
)
def test_is_external_change(
    previous: Setpoints | None, current: Setpoints, commanded: Setpoints | None, expected: bool
) -> None:
    # THEN
    assert is_external_change(previous, current, commanded) is expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (LiveMode.HEAT, Setpoints(72.0, 74.0)),
        (LiveMode.COOL, Setpoints(68.0, 78.0)),
        (LiveMode.HEAT_COOL, Setpoints(72.0, 78.0)),
    ],
)
def test_adopted_targets_keep_the_thermostat_where_it_was_put(mode: LiveMode, expected: Setpoints) -> None:
    # WHEN
    targets = adopted_targets(mode, Setpoints(69.0, 75.0), -3.0, Setpoints(68.0, 74.0))

    # THEN
    assert targets == expected


@pytest.mark.parametrize(
    ("mode", "temperature", "expected"),
    [
        (LiveMode.HEAT, 66.0, -2.0),
        (LiveMode.COOL, 76.0, 2.0),
        (LiveMode.HEAT_COOL, 66.0, -2.0),
        (LiveMode.HEAT_COOL, 75.0, 1.0),
        (LiveMode.HEAT_COOL, 70.0, 0.0),
        (LiveMode.NONE, 70.0, None),
        (LiveMode.HEAT, None, None),
    ],
)
def test_room_error(mode: LiveMode, temperature: float | None, expected: float | None) -> None:
    # THEN
    assert room_error(mode, temperature, Setpoints(68.0, 74.0)) == expected


def test_room_error_without_a_target_is_none() -> None:
    # THEN
    assert room_error(LiveMode.HEAT_COOL, 70.0, Setpoints(68.0, None)) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("living", Selection.room("living")),
        ("  OFFICE ", Selection.room("office")),
        ("Bed", Selection.room("bed")),
        ("Average", Selection(Strategy.AVERAGE)),
        ("extreme", Selection(Strategy.EXTREME)),
        ("garage", None),
        (7, None),
    ],
)
def test_parse_selection(value: object, expected: Selection | None) -> None:
    # THEN
    assert parse_selection(value, ROOMS) == expected


def test_parse_block_reads_every_key() -> None:
    # WHEN
    block = parse_block({"room": "bed", "heat": "66", "cool": 74}, ROOMS)

    # THEN
    assert block == Block(Selection.room("bed"), 66.0, 74.0, ())


def test_parse_block_with_no_keys_changes_nothing() -> None:
    # THEN
    assert parse_block({}, ROOMS) == Block()


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"room": "garage"}, "unknown room 'garage'"),
        ({"heat": "warm"}, "heat is not a number"),
        ({"cool": None}, "cool is not a number"),
        ({"heat": True}, "heat is not a number"),
        ({"cool": "nan"}, "cool is not a number"),
    ],
)
def test_parse_block_reports_invalid_values(data: dict[str, object], error: str) -> None:
    # WHEN
    block = parse_block(data, ROOMS)

    # THEN
    assert block.errors == (error,)
    assert (block.selection, block.heat, block.cool) == (None, None, None)
