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
| Rooms this thermostat doesn't heat or cool | none | Room sensors in parts of the house this system doesn't reach (e.g. a room with its own mini split). They are still reported (`room_thermostat/config` lists them with `followable: false`) but never followed: not selectable, not in presets or holds, not the reference or default room, and left out of `average` and `extreme`. |
| Fan speed selector | none | Optional. A select entity for the thermostat's fan speed (e.g. circulation speed); `room_thermostat/config` → `controls.fan_speed` tells apps which one to offer. |
| Outdoor temperature sensor | none | Optional. Offered to apps for history next to the rooms (`room_thermostat/config` → `history`); not used for control. |
| Default room | reference room, else `average` | Room id, `average` or `extreme`; used outside schedule blocks. |
| Schedule | none | A [Schedule helper](https://www.home-assistant.io/integrations/schedule/) (see below). |
| When the thermostat is changed outside Home Assistant | Hold | **Hold** respects the change for the hold duration, then room control resumes (a new schedule block or **Resume** ends it sooner). **Adopt** keeps the thermostat where it was put and moves the room target by the current offset. Either way a `room_thermostat_manual_change` event fires. |
| Hold duration | 120 min | A newer outside change restarts the clock. `0` holds until the next schedule block or **Resume**. |
| Sensor stale after | 10 min | |
| Maximum offset | 6 °F / 3.5 °C | |
| Deadband | 0.5 °F / 0.3 °C | |
| Minimum time between writes | 15 min | Keep this well above your thermostat integration's polling interval and cloud rate limits. |
| Offset smoothing | 15 min | |
| Setpoint step | 0.5 | |
| Minimum heat/cool range | 0 | Set this to your thermostat's heat/cool deadband if it enforces one (many do), so room ranges that it would reject are refused up front. |

### Presets, schedule and holds

Every room thermostat has presets: **Home**, **Away** and **Sleep** (built in; their targets and
rooms are editable, and Home starts from what the thermostat runs today) plus any custom ones, such
as *Movie night*. A preset is a pair of room targets and the room(s) to follow. Choose one with
`climate.set_preset_mode` (for example from a presence automation that switches to Away).

The weekly schedule is a list of (time → preset) blocks per day; a block stays in effect until the
next one, across midnight and the end of the week. Presets and the schedule are edited from an app
through the WebSocket API below, which every Home Assistant user may call (Schedule helpers can only
be edited by admins).

A change made through Home Assistant (targets, the active room, a preset) is a **hold** that
outranks the schedule: by default until the next block; or for a number of minutes (it then returns
to the schedule, or to what ran before if there is none); or until someone resumes. Without a
schedule, a new target or room is simply the new setting, while a preset is held until resumed, so
presets work as toggles over the normal setting (Resume returns to it). **Resume** ends
any hold and returns to the schedule. A `room_thermostat_override_ended` event fires when such a hold
ends (`reason`: `expired`, `resumed` or `schedule`).

#### WebSocket API

Each command takes `entity_id` (the room thermostat's climate entity) and answers with the
thermostat's config: `revision`, `unit`, `setpoint_step`, `minimum_range`, `rooms` (id, name, sensor
`entity_id`, `humidity_entity_id` when a matching `sensor.<room>_humidity` exists, `followable`), `presets`, `schedule`, `active_preset`, `hold`, `selection`, `controls` (`fan_speed`: an optional select entity), and `history` (the entity
ids an app charts: room, thermostat and commanded temperatures, the outdoor sensor, and the thermostat for its
heating/cooling activity). The climate entity's `config_revision` attribute
changes whenever presets or the schedule do.

| Command | Data |
| --- | --- |
| `room_thermostat/config` | |
| `room_thermostat/presets/save` | `presets`: the full list, `[{id?, name, heat, cool, room}]`; omit `id` for a new preset. Built-ins cannot be removed, nor presets the schedule uses. |
| `room_thermostat/schedule/save` | `schedule`: seven day lists, Monday first, of `{time: "HH:MM", preset: id}`. |
| `room_thermostat/set` | Any of `preset`, `heat`, `cool`, `room`, plus `hold`: `{kind: next_block}` (default), `{kind: minutes, minutes: N}` or `{kind: indefinite}`. |
| `room_thermostat/resume` | |

#### Schedule helper (alternative)

Instead of the built-in schedule, a Schedule helper can drive the thermostat; it is used only while
the built-in schedule is empty. On any time block, open **Additional data** and set any of:

```yaml
room: bedroom   # a room id or name, or average / extreme
heat: 66        # room heat target
cool: 74        # room cool target
```

When a block starts, its values are applied and any hold ends. Keys a block leaves
out keep their current value. Outside blocks, the default room applies. Invalid values are skipped
and listed on the *Control problem* sensor.

## Entities

| Entity | |
| --- | --- |
| `climate.<name>` | The room thermostat: current temperature is the room's, targets are room targets (single in heat/cool, a range in heat_cool). Modes, fan modes, humidity, action and limits mirror the real thermostat, and mode and fan changes pass straight through to it. Preset modes are the presets. Attributes include `preset_id`, `override` (the current hold: `kind`, `until`), `schedule_next_change` and `config_revision`. |
| `select.<name>_active_room` | Each room, *Average of all rooms*, *Room most off target* (coldest when heating, hottest when cooling, furthest outside the range in heat_cool). A choice stands until the next schedule block. |
| `sensor.<name>_room_temperature` | The temperature being controlled to. |
| `sensor.<name>_room_error` | Room temperature minus its target (0 inside a heat_cool range). |
| `sensor.<name>_room_offset` | The offset applied. Diagnostic. |
| `sensor.<name>_thermostat_humidity` | The real thermostat's humidity, when it reports one (long-term statistics). Diagnostic. |
| `sensor.<name>_thermostat_temperature` | The real thermostat's own reading (long-term statistics, for history next to the rooms). Diagnostic. |
| `sensor.<name>_commanded_heat_setpoint` / `_commanded_cool_setpoint` | What the real thermostat should be set to (long-term statistics). Diagnostic. |
| `sensor.<name>_schedule` | `not_configured` (no schedule: a normal state), `in_block` (always, with the built-in schedule), `between_blocks`, or `not_found` (a Schedule helper that is configured but missing, also a control problem). Attributes: `schedule_entity`, `next_change`. |
| `sensor.<name>_hold_ends` | When the current manual hold ends (unknown when not holding). |
| `sensor.<name>_control_state` | `controlling`, `fallback_reference`, `fallback_thermostat`, `manual_hold`, `idle`, `underlying_unavailable`. Diagnostic. |
| `binary_sensor.<name>_control_problem` | On during any fallback, schedule data error, failed write or unavailable thermostat; `reasons` lists them. |
| `button.<name>_resume` | Ends a hold and returns to the schedule. |

### Alerts

The integration raises no notifications itself. Automate on `binary_sensor.<name>_control_problem`
(and its `reasons`) and on two events:

- `room_thermostat_manual_change`: `policy`, `mode`, `heat`, `cool`, and `hold_until` (ISO time, or
  null when holding indefinitely or adopting).
- `room_thermostat_hold_ended`: `reason` is `expired`, `resumed` or `schedule`.
- `room_thermostat_override_ended`: a hold set through Home Assistant ended; same reasons.

"Outside Home Assistant" means any setpoint change this integration did not make: the vendor's app,
the thermostat's own screen, or the underlying climate entity. It is noticed at the underlying
integration's next poll.

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
