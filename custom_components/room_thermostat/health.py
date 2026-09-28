"""Sensor health: learn how each room sensor normally behaves, and notice when it doesn't.

Pure rules, no Home Assistant state. A baseline is learned per sensor from its hourly statistics (mean, min, max):
for each hour of the day, the band its hourly means usually fall in, and how much it usually moves within an hour.
A sensor then looks wrong when it is **stale** (no readings), **flat** (the same value for hours though it normally
moves), **jumpy** (a change between readings far bigger than it ever makes) or **unusual** (well outside its band for
that hour, for a while). Rooms swing differently, so every threshold is relative to that sensor's own history.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median
from typing import Final

#: A baseline needs this many days of hourly statistics before flat / jumpy / unusual are judged.
MIN_LEARNING_DAYS: Final = 7
#: No reading for this long is stale, baseline or not.
STALE_AFTER: Final = timedelta(minutes=30)
#: The same value for this long, when the sensor usually moves, is flat.
FLAT_AFTER: Final = timedelta(hours=4)
#: Outside the band for this long is unusual.
UNUSUAL_AFTER: Final = timedelta(minutes=45)
#: A jumpy reading keeps the issue open this long, so one spike is not a flapping alert.
JUMPY_HOLD: Final = timedelta(hours=1)


class IssueKind(StrEnum):
    STALE = "stale"
    FLAT = "flat"
    JUMPY = "jumpy"
    UNUSUAL = "unusual"


@dataclass(frozen=True)
class HourlyStat:
    """One hour of a sensor's long-term statistics (start is local time)."""

    start: datetime
    mean: float
    low: float
    high: float


@dataclass(frozen=True)
class Baseline:
    """How a sensor normally behaves."""

    #: Hour of day (0-23) -> (low, high) its hourly means usually fall in.
    bands: dict[int, tuple[float, float]]
    #: Its median movement within an hour (max - min).
    movement: float
    days: int

    @property
    def learned(self) -> bool:
        return self.days >= MIN_LEARNING_DAYS

    def band(self, hour: int, margin: float) -> tuple[float, float] | None:
        if hour not in self.bands:
            return None
        low, high = self.bands[hour]
        widen = max(margin, (high - low) * 0.5)
        return low - widen, high + widen

    def jump_threshold(self, floor: float) -> float:
        """A change between two readings bigger than this is not how the sensor moves."""
        return max(floor, self.movement * 4)


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def learn(stats: Iterable[HourlyStat]) -> Baseline:
    """A baseline from hourly statistics: per hour of day, the 5th-95th percentile of its hourly means."""
    by_hour: dict[int, list[float]] = {}
    movements: list[float] = []
    days: set[tuple[int, int, int]] = set()
    for stat in stats:
        by_hour.setdefault(stat.start.hour, []).append(stat.mean)
        movements.append(max(0.0, stat.high - stat.low))
        days.add((stat.start.year, stat.start.month, stat.start.day))
    bands = {
        hour: (_quantile(means, 0.05), _quantile(means, 0.95)) for hour, means in by_hour.items() if len(means) >= 3
    }
    return Baseline(bands=bands, movement=median(movements) if movements else 0.0, days=len(days))


@dataclass(frozen=True)
class Reading:
    """What a sensor reports right now."""

    value: float | None
    #: When it last reported anything (even an unchanged value).
    reported: datetime | None
    #: When its value last changed.
    changed: datetime | None


@dataclass
class SensorWatch:
    """The running checks for one sensor: a baseline plus what it has recently done."""

    #: Floor for a jump and minimum band margin, in the sensor's unit (e.g. 3 °F, 8 %).
    jump_floor: float
    margin: float
    baseline: Baseline | None = None
    outside_since: datetime | None = None
    jumpy_until: datetime | None = None
    jump_reason: str = ""
    last_value: float | None = None
    issues: dict[IssueKind, str] = field(default_factory=dict)

    def observe(self, value: float | None, now: datetime) -> None:
        """A new reading: remember it, and flag a jump far bigger than the sensor ever moves."""
        previous = self.last_value
        self.last_value = value
        baseline = self.baseline
        if value is None or previous is None or baseline is None or not baseline.learned:
            return
        change = value - previous
        if abs(change) > baseline.jump_threshold(self.jump_floor):
            self.jumpy_until = now + JUMPY_HOLD
            self.jump_reason = (
                f"jumped {change:+.1f} between readings; it usually moves {baseline.movement:.1f} an hour"
            )

    def check(self, reading: Reading, now: datetime, local_hour: int) -> dict[IssueKind, str]:
        """The issues this sensor has right now, each with a human reason."""
        issues: dict[IssueKind, str] = {}
        if reading.reported is None or now - reading.reported >= STALE_AFTER:
            minutes = None if reading.reported is None else int((now - reading.reported).total_seconds() // 60)
            issues[IssueKind.STALE] = "no readings" if minutes is None else f"no readings for {minutes} minutes"
            self.outside_since = None
            self.issues = issues
            return issues
        baseline = self.baseline
        if baseline is None or not baseline.learned or reading.value is None:
            self.issues = issues
            return issues
        if (
            reading.changed is not None
            and now - reading.changed >= FLAT_AFTER
            and baseline.movement >= self.jump_floor / 10
        ):
            hours = (now - reading.changed).total_seconds() / 3600
            issues[IssueKind.FLAT] = (
                f"stuck at {reading.value:g} for {hours:.0f} hours; it usually moves {baseline.movement:.1f} an hour"
            )
        if self.jumpy_until is not None and now < self.jumpy_until:
            issues[IssueKind.JUMPY] = self.jump_reason
        band = baseline.band(local_hour, self.margin)
        if band is not None and not band[0] <= reading.value <= band[1]:
            self.outside_since = self.outside_since or now
            if now - self.outside_since >= UNUSUAL_AFTER:
                issues[IssueKind.UNUSUAL] = (
                    f"{reading.value:g} is outside its usual {band[0]:.0f}-{band[1]:.0f} for this time of day"
                )
        else:
            self.outside_since = None
        self.issues = issues
        return issues
