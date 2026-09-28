from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.room_thermostat.health import (
    FLAT_AFTER,
    STALE_AFTER,
    UNUSUAL_AFTER,
    Baseline,
    HourlyStat,
    IssueKind,
    Reading,
    SensorWatch,
    learn,
)

NOW = datetime(2026, 9, 27, 14, tzinfo=UTC)


def _history(days: int, level: float = 68.0, swing: float = 1.0, movement: float = 0.6) -> list[HourlyStat]:
    """Hourly stats for [days] days: a daily rhythm of +/- swing around [level]."""
    stats = []
    start = NOW - timedelta(days=days)
    for hour in range(days * 24):
        at = start + timedelta(hours=hour)
        wobble = swing * ((hour % 24) - 12) / 12
        mean = level + wobble + (0.2 if hour % 2 else -0.2)
        stats.append(HourlyStat(at, mean, mean - movement / 2, mean + movement / 2))
    return stats


def _watch(days: int = 14, **kwargs: float) -> SensorWatch:
    watch = SensorWatch(jump_floor=3.0, margin=2.0)
    watch.baseline = learn(_history(days, **kwargs))
    return watch


def _fresh(value: float, changed_ago: timedelta = timedelta(minutes=5), at: datetime = NOW) -> Reading:
    return Reading(value=value, reported=at - timedelta(minutes=1), changed=at - changed_ago)


def test_a_baseline_learns_a_band_per_hour_and_how_much_the_sensor_moves() -> None:
    # WHEN
    baseline = learn(_history(14))

    # THEN
    assert baseline.days == 15
    assert baseline.learned
    assert baseline.movement == pytest.approx(0.6)
    low, high = baseline.bands[14]
    assert 66.5 <= low <= high <= 69.5, "within the daily +/- 1 swing and its +/- 0.2 wobble"


def test_too_little_history_is_still_learning() -> None:
    # THEN
    assert not learn(_history(3)).learned
    assert learn([]).movement == 0.0


def test_a_healthy_sensor_has_no_issues() -> None:
    # WHEN
    issues = _watch().check(_fresh(68.2), NOW, local_hour=14)

    # THEN
    assert issues == {}


def test_no_readings_is_stale_even_while_learning() -> None:
    # GIVEN
    watch = SensorWatch(jump_floor=3.0, margin=2.0)

    # WHEN
    silent = watch.check(Reading(68, NOW - STALE_AFTER - timedelta(minutes=2), NOW), NOW, 14)
    never = watch.check(Reading(None, None, None), NOW, 14)

    # THEN
    assert silent == {IssueKind.STALE: "no readings for 32 minutes"}
    assert never == {IssueKind.STALE: "no readings"}


def test_the_same_value_for_hours_is_flat_when_the_sensor_usually_moves() -> None:
    # WHEN
    stuck = _watch().check(_fresh(68.2, changed_ago=FLAT_AFTER + timedelta(hours=1)), NOW, 14)
    still = _watch(movement=0.0).check(_fresh(68.2, changed_ago=FLAT_AFTER * 2), NOW, 14)

    # THEN
    assert "stuck at 68.2 for 5 hours" in stuck[IssueKind.FLAT]
    assert still == {}, "a sensor that never moves is not flat"


def test_a_jump_far_bigger_than_the_sensor_moves_is_jumpy_for_a_while() -> None:
    # GIVEN
    watch = _watch()
    watch.observe(68.0, NOW - timedelta(minutes=2))

    # WHEN
    watch.observe(75.0, NOW - timedelta(minutes=1))
    issues = watch.check(_fresh(75.0), NOW, 14)

    # THEN
    assert "jumped +7.0" in issues[IssueKind.JUMPY]
    assert IssueKind.JUMPY not in watch.check(_fresh(68.0), NOW + timedelta(hours=2), 14)


def test_small_changes_and_changes_while_learning_are_not_jumps() -> None:
    # GIVEN
    watch = _watch()
    learning = _watch(days=3)

    # WHEN
    watch.observe(68.0, NOW)
    watch.observe(69.5, NOW)
    learning.observe(60.0, NOW)
    learning.observe(75.0, NOW)

    # THEN
    assert watch.jumpy_until is None
    assert learning.jumpy_until is None


def test_outside_its_band_for_a_while_is_unusual() -> None:
    # GIVEN
    watch = _watch()

    # WHEN
    later_at = NOW + UNUSUAL_AFTER
    first = watch.check(_fresh(80.0), NOW, 14)
    later = watch.check(_fresh(80.0, at=later_at), later_at, 14)
    band = watch.baseline.band(14, 2.0) if watch.baseline else (0.0, 0.0)
    back = watch.check(_fresh((band[0] + band[1]) / 2, at=later_at), later_at, 14)

    # THEN
    assert first == {}, "a brief excursion is not unusual yet"
    assert "80 is outside its usual" in later[IssueKind.UNUSUAL]
    assert back == {}


def test_an_hour_without_a_band_is_not_judged() -> None:
    # GIVEN
    watch = SensorWatch(jump_floor=3.0, margin=2.0)
    watch.baseline = Baseline(bands={}, movement=0.5, days=10)

    # WHEN
    later_at = NOW + UNUSUAL_AFTER * 2
    watch.check(_fresh(99.0), NOW, 14)
    issues = watch.check(_fresh(99.0, at=later_at), later_at, 14)

    # THEN
    assert issues == {}
