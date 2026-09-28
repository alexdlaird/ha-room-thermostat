"""Runs the sensor-health rules (health.py) against Home Assistant: every room sensor, temperature and humidity.

Baselines are learned from the recorder's hourly statistics at start and daily; readings are checked on every change
and once a minute. A new issue fires `room_thermostat_sensor_issue` (and so does its clearing), and every issue is kept
in a 30-day log for apps to show on history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_SENSOR_ISSUE
from .health import HourlyStat, IssueKind, Reading, SensorWatch, learn

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

LOG_DAYS: Final = 30
LEARN_DAYS: Final = 30
CHECK_INTERVAL: Final = timedelta(minutes=1)
RELEARN_INTERVAL: Final = timedelta(hours=24)
STORAGE_VERSION: Final = 1

#: (jump floor, band margin) by what a sensor measures: °F, °C, humidity %.
LIMITS: Final[dict[str, tuple[float, float]]] = {"°F": (3.0, 2.0), "°C": (1.7, 1.1), "%": (10.0, 6.0)}

type StatsFetcher = Callable[[list[str], datetime, datetime], Awaitable[dict[str, list[HourlyStat]]]]


@dataclass(frozen=True)
class Watched:
    entity_id: str
    name: str
    #: Unit the thresholds are in: "°F", "°C" or "%".
    unit: str


class HealthMonitor:
    """Watches room sensors and keeps their issues and an issue log."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sensors: list[Watched],
        fetch_stats: StatsFetcher | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.sensors = {sensor.entity_id: sensor for sensor in sensors}
        self._fetch = fetch_stats or self._recorder_stats
        self._watches = {sensor.entity_id: SensorWatch(*LIMITS.get(sensor.unit, LIMITS["°C"])) for sensor in sensors}
        self._open: dict[tuple[str, IssueKind], dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.health")

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.log = [record for record in stored.get("log", []) if isinstance(record, dict)]
        for record in self.log:
            if record.get("end") is None and record.get("entity_id") in self.sensors:
                kind = record.get("kind")
                if kind in {k.value for k in IssueKind}:
                    self._open[(record["entity_id"], IssueKind(kind))] = record
        for entity_id, watch in self._watches.items():
            watch.last_value = self._value(self.hass.states.get(entity_id))
        self._unsubscribe = [
            async_track_state_change_event(self.hass, list(self.sensors), self._async_on_change),
            async_track_time_interval(self.hass, self._async_on_tick, CHECK_INTERVAL),
            async_track_time_interval(self.hass, self._async_relearn, RELEARN_INTERVAL),
        ]
        self.hass.async_create_background_task(self.async_learn(), f"{DOMAIN} learn sensor baselines")

    @callback
    def async_stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @property
    def active(self) -> list[dict[str, Any]]:
        """Open issues, newest first."""
        return sorted(self._open.values(), key=lambda record: record["start"], reverse=True)

    async def async_learn(self) -> None:
        """(Re)learn every sensor's baseline from the last 30 days of hourly statistics."""
        end = dt_util.utcnow()
        try:
            stats = await self._fetch(list(self.sensors), end - timedelta(days=LEARN_DAYS), end)
        except Exception:
            _LOGGER.warning("Could not read sensor statistics; sensor health checks run without baselines")
            return
        for entity_id, watch in self._watches.items():
            watch.baseline = learn(stats.get(entity_id, []))
        self.evaluate()

    @callback
    def _async_relearn(self, _now: datetime) -> None:
        self.hass.async_create_background_task(self.async_learn(), f"{DOMAIN} relearn sensor baselines")

    @callback
    def _async_on_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        watch = self._watches.get(entity_id)
        if watch is not None:
            watch.observe(self._value(event.data["new_state"]), dt_util.utcnow())
        self.evaluate()

    @callback
    def _async_on_tick(self, _now: datetime) -> None:
        self.evaluate()

    @callback
    def evaluate(self) -> None:
        """Check every sensor; open and close issues, telling listeners and firing events."""
        now = dt_util.utcnow()
        hour = dt_util.as_local(now).hour
        changed = False
        for entity_id, watch in self._watches.items():
            state = self.hass.states.get(entity_id)
            reading = Reading(
                value=self._value(state),
                reported=None if state is None else state.last_reported,
                changed=None if state is None else state.last_changed,
            )
            issues = watch.check(reading, now, hour)
            for kind, reason in issues.items():
                if (entity_id, kind) not in self._open:
                    self._raise(entity_id, kind, reason, now, watch.since.get(kind))
                    changed = True
            for key in [key for key in self._open if key[0] == entity_id and key[1] not in issues]:
                self._clear(key, now)
                changed = True
        if changed:
            self._prune(now)
            self._store.async_delay_save(lambda: {"log": self.log}, 5)
            for update in list(self._listeners):
                update()

    def _raise(self, entity_id: str, kind: IssueKind, reason: str, now: datetime, since: datetime | None) -> None:
        sensor = self.sensors[entity_id]
        record = {
            "entity_id": entity_id,
            "name": sensor.name,
            "kind": kind.value,
            "reason": reason,
            "start": now.isoformat(),
            "since": None if since is None else since.isoformat(),
            "end": None,
        }
        self._open[(entity_id, kind)] = record
        self.log.append(record)
        self._fire(record, cleared=False)

    def _clear(self, key: tuple[str, IssueKind], now: datetime) -> None:
        record = self._open.pop(key)
        record["end"] = now.isoformat()
        self._fire(record, cleared=True)

    def _fire(self, record: Mapping[str, Any], *, cleared: bool) -> None:
        self.hass.bus.async_fire(
            EVENT_SENSOR_ISSUE,
            {
                "entry_id": self.entry.entry_id,
                "entity_id": record["entity_id"],
                "name": record["name"],
                "kind": record["kind"],
                "reason": record["reason"],
                "cleared": cleared,
            },
        )

    def _prune(self, now: datetime) -> None:
        oldest = (now - timedelta(days=LOG_DAYS)).isoformat()
        self.log = [record for record in self.log if record["end"] is None or record["end"] >= oldest]

    @staticmethod
    def _value(state: Any) -> float | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except TypeError, ValueError:
            return None

    async def _recorder_stats(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[HourlyStat]]:
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415
        from homeassistant.helpers.recorder import get_instance  # noqa: PLC0415 - recorder is optional

        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(entity_ids),
            "hour",
            None,
            {"mean", "min", "max"},
        )
        return {entity_id: [stat for row in entries if (stat := _hourly(row))] for entity_id, entries in rows.items()}


def _hourly(row: Mapping[str, Any]) -> HourlyStat | None:
    mean, low, high = row.get("mean"), row.get("min"), row.get("max")
    if mean is None or low is None or high is None:
        return None
    return HourlyStat(dt_util.as_local(dt_util.utc_from_timestamp(float(row["start"]))), mean, low, high)
