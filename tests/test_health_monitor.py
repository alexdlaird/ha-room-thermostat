from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.room_thermostat.const import EVENT_SENSOR_ISSUE
from custom_components.room_thermostat.health import STALE_AFTER, UNUSUAL_AFTER, HourlyStat
from custom_components.room_thermostat.health_monitor import HealthMonitor, Watched

from .conftest import LIVING, ROOM_CLIMATE, make_entry, set_rooms, set_thermostat, setup_entry

LIVING_HUMIDITY = "sensor.living_humidity"


def _steady_history(level: float) -> list[HourlyStat]:
    start = dt_util.now() - timedelta(days=14)
    stats = []
    for hour in range(14 * 24):
        mean = level + (0.3 if hour % 2 else -0.3)
        stats.append(HourlyStat(start + timedelta(hours=hour), mean, mean - 0.4, mean + 0.4))
    return stats


async def _fetch(_ids: list[str], _start: datetime, _end: datetime) -> dict[str, list[HourlyStat]]:
    return {LIVING: _steady_history(66.0), LIVING_HUMIDITY: _steady_history(45.0)}


@pytest.fixture(autouse=True)
def stop_monitors() -> Generator[list[HealthMonitor]]:
    started: list[HealthMonitor] = []
    yield started
    for monitor in started:
        monitor.async_stop()


async def _monitor(hass: HomeAssistant, started: list[HealthMonitor]) -> tuple[HealthMonitor, list[Event]]:
    entry = MockConfigEntry(domain="room_thermostat")
    entry.add_to_hass(hass)
    events: list[Event] = []
    hass.bus.async_listen(EVENT_SENSOR_ISSUE, events.append)
    monitor = HealthMonitor(
        hass,
        entry,
        [Watched(LIVING, "Living", "°F"), Watched(LIVING_HUMIDITY, "Living humidity", "%")],
        fetch_stats=_fetch,
    )
    await monitor.async_start()
    started.append(monitor)
    await hass.async_block_till_done()
    return monitor, events


async def test_a_silent_sensor_raises_a_stale_issue_and_clears_when_it_reports(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, stop_monitors: list[HealthMonitor]
) -> None:
    # GIVEN
    hass.states.async_set(LIVING, "66.1")
    hass.states.async_set(LIVING_HUMIDITY, "45")
    monitor, events = await _monitor(hass, stop_monitors)

    # WHEN
    freezer.tick(STALE_AFTER + timedelta(minutes=1))
    hass.states.async_set(LIVING_HUMIDITY, "45.5")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    stale = list(monitor.active)
    hass.states.async_set(LIVING, "66.2")
    await hass.async_block_till_done()

    # THEN
    assert [(issue["entity_id"], issue["kind"]) for issue in stale] == [(LIVING, "stale")]
    assert monitor.active == []
    assert [(event.data["name"], event.data["cleared"]) for event in events] == [("Living", False), ("Living", True)]
    assert monitor.log[0]["end"] is not None, "the cleared issue stays in the log with its end"


async def test_a_reading_well_outside_its_learned_band_becomes_unusual(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, stop_monitors: list[HealthMonitor]
) -> None:
    # GIVEN
    hass.states.async_set(LIVING, "66.1")
    hass.states.async_set(LIVING_HUMIDITY, "45")
    monitor, _events = await _monitor(hass, stop_monitors)

    # WHEN
    hass.states.async_set(LIVING, "80")
    hass.states.async_set(LIVING_HUMIDITY, "45.2")
    await hass.async_block_till_done()
    freezer.tick(UNUSUAL_AFTER + timedelta(minutes=1))
    hass.states.async_set(LIVING, "80.1")
    hass.states.async_set(LIVING_HUMIDITY, "45.1")
    await hass.async_block_till_done()

    # THEN
    kinds = {issue["kind"] for issue in monitor.active if issue["entity_id"] == LIVING}
    assert kinds == {"jumpy", "unusual"}
    assert all(issue["entity_id"] == LIVING for issue in monitor.active), "humidity stayed normal"


async def test_the_issue_log_survives_a_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, stop_monitors: list[HealthMonitor]
) -> None:
    # GIVEN
    hass.states.async_set(LIVING, "66.1")
    hass.states.async_set(LIVING_HUMIDITY, "45")
    monitor, _events = await _monitor(hass, stop_monitors)
    freezer.tick(STALE_AFTER + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # WHEN
    restarted = HealthMonitor(hass, monitor.entry, list(monitor.sensors.values()), fetch_stats=_fetch)
    await restarted.async_start()
    stop_monitors.append(restarted)

    # THEN
    assert {issue["entity_id"] for issue in restarted.active} == {LIVING, LIVING_HUMIDITY}


async def test_the_integration_watches_room_and_humidity_sensors_and_reports_issues(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, thermostat_calls: AsyncMock
) -> None:
    # GIVEN
    set_thermostat(hass, "heat", temperature=68.0)
    set_rooms(hass, living=66.0, office=65.0, bed=60.0)
    hass.states.async_set(LIVING_HUMIDITY, "45")
    entry = await setup_entry(hass, make_entry())
    client = await hass_ws_client(hass)

    # WHEN
    await client.send_json_auto_id({"type": "room_thermostat/sensor_issues", "entity_id": ROOM_CLIMATE})
    response: dict[str, Any] = await client.receive_json()

    # THEN
    watched = {sensor.entity_id: sensor.name for sensor in entry.runtime_data.watched_sensors()}
    assert watched[LIVING] == "Living"
    assert watched[LIVING_HUMIDITY] == "Living humidity"
    assert response["result"] == {"active": [], "log": []}
    assert hass.states.get("sensor.house_room_sensor_issues").state == "0"
