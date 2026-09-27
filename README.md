# Room Thermostat for Home Assistant

[![CI](https://github.com/alexdlaird/ha-room-thermostat/actions/workflows/ci.yml/badge.svg)](https://github.com/alexdlaird/ha-room-thermostat/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Control any thermostat to the temperature of **the room that matters**, the way remote room
sensors work on thermostats that have them. Pick a room (or the average, or whichever room is
furthest off), set room targets, and this integration steers your existing thermostat to get that
room there.

It never runs your equipment directly. It measures the gap between the thermostat's own reading and
the room (the *offset*), shifts the thermostat's setpoints by that gap, and lets the thermostat keep
doing what it is good at: staging, cycle protection, heat/cool changeover. If the room sensors go
quiet, it falls back gracefully and tells you.

## How it works

1. **Room temperature.** Each configured room sensor that reported recently (default: within 10
   minutes) is *fresh*. The active selection (one room, the average, or the room most off target)
   is computed over the fresh rooms.
2. **Offset.** For every fresh room the integration keeps a smoothed (time-weighted moving average,
   default 15 minutes) value of *thermostat reading minus room reading*, capped at a maximum
   (default 6 °F / 3.5 °C).
3. **Commanded setpoints.** Room targets plus the offset, rounded to the setpoint step and clamped
   to the thermostat's limits. In heat_cool both sides move by the same amount, so the range width
   is preserved.
4. **Writing.** The thermostat is only rewritten when a commanded setpoint differs from its current
   one by at least the deadband (default 0.5 °F / 0.3 °C), and routine corrections wait at least
   the minimum write interval (default 15 minutes). Changing a target, the mode, the room, or a new
   schedule block writes right away.
5. **Fallbacks.** Active room stale → the reference room (a sensor next to the thermostat) → no
   fresh room at all: the reference room's last learned offset, so the thermostat's own bias is
   still corrected. Every fallback turns the *Control problem* sensor on with a reason.

Off, fan only and other modes without setpoints are left alone.

## Installation

HACS → ⋮ → **Custom repositories** → `https://github.com/alexdlaird/ha-room-thermostat`, type
**Integration** → download the latest release → restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → Room Thermostat**:

| Field | |
| --- | --- |
| Name | Becomes the device and the thermostat entity name. |
| Thermostat | The real `climate` entity to steer. One room thermostat per thermostat. |
| Room temperature sensors | One per room. A room's id is the sensor's entity id without the domain and a trailing `_temperature` (`sensor.bedroom_temperature` → `bedroom`); its display name is the sensor's name without a trailing "Temperature". |
| Reference room | Optional. A sensor mounted next to the thermostat. Used when the active room goes stale, and its offset is remembered as the thermostat's own error. |

Everything else is under **Configure** (saving reloads the integration):

| Option | Default | |
| --- | --- | --- |
| Default room | reference room, else `average` | Room id, `average` or `extreme`; used outside schedule blocks. |
| Schedule | none | A [Schedule helper](https://www.home-assistant.io/integrations/schedule/) (see below). |
| When the thermostat is changed outside Home Assistant | Hold | **Hold** stops writing until the next schedule block or **Resume**. **Adopt** keeps the thermostat where it was put and moves the room target by the current offset. Either way a `room_thermostat_manual_change` event fires. |
| Sensor stale after | 10 min | |
| Maximum offset | 6 °F / 3.5 °C | |
| Deadband | 0.5 °F / 0.3 °C | |
| Minimum time between writes | 15 min | Keep this well above your thermostat integration's polling interval and cloud rate limits. |
| Offset smoothing | 15 min | |
| Setpoint step | 0.5 | |
| Minimum heat/cool range | 0 | Set this to your thermostat's heat/cool deadband if it enforces one (many do), so room ranges that it would reject are refused up front. |

### Schedules

Add a Schedule helper and, on any time block, open **Additional data** and set any of:

```yaml
room: bedroom   # a room id or name, or average / extreme
heat: 66        # room heat target
cool: 74        # room cool target
```

When a block starts, its values are applied and any hold or manual choice ends. Keys a block leaves
out keep their current value. Outside blocks, the default room applies. Invalid values are skipped
and listed on the *Control problem* sensor.

## Entities

| Entity | |
| --- | --- |
| `climate.<name>` | The room thermostat: current temperature is the room's, targets are room targets (single in heat/cool, a range in heat_cool). Modes, action and limits mirror the real thermostat, and mode changes pass straight through to it. |
| `select.<name>_active_room` | Each room, *Average of all rooms*, *Room most off target* (coldest when heating, hottest when cooling, furthest outside the range in heat_cool). A choice stands until the next schedule block. |
| `sensor.<name>_room_temperature` | The temperature being controlled to. |
| `sensor.<name>_room_error` | Room temperature minus its target (0 inside a heat_cool range). |
| `sensor.<name>_room_offset` | The offset applied. Diagnostic. |
| `sensor.<name>_commanded_heat_setpoint` / `_commanded_cool_setpoint` | What the real thermostat should be set to. Diagnostic. |
| `sensor.<name>_control_state` | `controlling`, `fallback_reference`, `fallback_thermostat`, `manual_hold`, `idle`, `underlying_unavailable`. Diagnostic. |
| `binary_sensor.<name>_control_problem` | On during any fallback, schedule data error, failed write or unavailable thermostat; `reasons` lists them. |
| `button.<name>_resume` | Ends a hold. |

### Alerts

The integration raises no notifications itself. Automate on `binary_sensor.<name>_control_problem`
(and its `reasons`) and on the `room_thermostat_manual_change` event, which carries `policy`, `mode`,
`heat` and `cool`.

## Trying it without touching the thermostat

Every write goes through `climate.set_temperature` on the real thermostat. If your thermostat
integration has a dry-run or read-only option, turn it on first: the room thermostat then runs
exactly as it will live, and you can compare the commanded setpoint sensors with the rooms before
letting it steer.

## Development

```bash
uv sync
make check   # ruff, mypy (strict), pytest with a 95 % coverage gate, privacy scan, hassfest
```

`scripts/privacy_scan.sh` fails if any term from a local, untracked `.private-terms` file appears
in the repository, so house-specific names never land here.
