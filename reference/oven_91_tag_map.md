# Small oven (`10.4.20.91`) - tag map

`.91` is the **small** oven; `.93` is the **large** oven this folder is named for.

Discovered 2026-08-26 by read-only `GetTagList` + UDT template expansion + live reads
via pylogix 1.0.6. 217 tags: 121 scalar, 96 structured.

Unlike `.93` (flat scalars like `Z1_ACTUAL_TEMP`), this PLC keeps most oven state inside
named UDTs. The useful ones:

| Tag | UDT type | Holds |
|-----|----------|-------|
| `BURNER_1`, `BURNER_2` | `OVEN_GAS_TRAIN` | per-burner temp, setpoint, flame, purge, faults |
| `OVEN_LOAD_CYCLE` | `LOAD_CYCLE` | load temp, cycle time remaining, step timing |
| `OVEN_EXHAUST` | `FREQ_DRIVE_MOTOR` | exhaust VFD run state, frequency, amperage |
| `RECIRC_FAN` | `OVEN_FAN` | recirculation fan run state + faults |
| `OVEN_DOORS`, `OVEN_ENTRANCE_DOOR`, `OVEN_EXIT_DOOR` | `OVEN_DOOR` | door position/limits |
| `OVEN_RECIPE*` | `Recipe` | recipe definition / active / compare |

## Confirmed by live read

Values in parentheses are what was actually read at 2026-08-26 ~08:35, with the oven
cold and idle - they confirm the tag is readable and plausible, not that the oven was
in any particular state.

### Temperature
- `BURNER_1.ACTUAL_TEMPERATURE_WIRE_1` REAL (102.7) - zone 1 raw temp
- `BURNER_1.CALIBRATED_TEMPERATURE` REAL (99.7) - zone 1 calibrated
- `BURNER_2.ACTUAL_TEMPERATURE_WIRE_1` REAL (105.9) - zone 2 raw temp
- `BURNER_2.CALIBRATED_TEMPERATURE` REAL (103.9) - zone 2 calibrated
- `BURNER_1.SET_POINT_FROM_MMI` REAL (250.0) - zone 1 setpoint
- `BURNER_2.SET_POINT_FROM_MMI` REAL (305.0) - zone 2 setpoint
- `BURNER_CONTROL_TEMPERATURE` REAL (99.9) - top-level control temp, tracks burner 1
- `USE_B1_TCOUPLE` / `USE_B2_TCOUPLE` BOOL (True / **False**)

`ACTUAL_TEMPERATURE_WIRE_2` reads 0.0 on both burners - second thermocouple wire
unused. Prefer `CALIBRATED_TEMPERATURE` for display; it applies
`BURNER_n_TC_CALIBRATION` (-3.0 / -2.0).

### Burner / flame state
- `.PILOT_ON`, `.MAIN_FLAME_ON`, `.FLAME_ON`, `.FLAME_ENABLE` BOOL
- `.BURNER_AT_LOW_FIRE`, `.BURNER_AT_HIGH_FIRE` BOOL - **see caveats**
- `.PURGING`, `.PURGE_COMPLETE` BOOL; `.PURGE_TIME_REMAINING` DINT (900)
- `.SCALED_CONTROL_VARIABLE` DINT - firing rate, analog of `.93`'s `ZONE_n_BURNER_MTR`
- `BURNER_1_SCALED_PID_OUT` / `BURNER_2_SCALED_PID_OUT` REAL - top-level PID output
- `PID_ACTIVE`, `FLAME_ON` BOOL - top-level rollups

### Fault bits (per burner)
`.HIGH_TEMPERATURE_FAULT`, `.FLAME_FAILURE_FAULT`, `.FLAME_FAILED_TO_LIGHT_FAULT`,
`.OPEN_THERMOCOUPLE_FAULT`, `.MAIN_GAS_VALVE_FAILED_OPEN_CLOSE_FAULT`,
`.HEAT_HOUSE_DOOR_OPEN_FAULT`, `.BLOCKING_VALVE_FAILED_TO_CLOSE_FAULT`

Gas/limit permissives: `.LOW_GAS_PRESS_OK`, `.HIGH_GAS_PRESS_OK`, `.HIGH_LIMIT_OK`,
`.MAIN_GAS_VALVE_FULL_OPEN`

Top-level: `FAULT_ACTIVE`, `FAULTS_TO_MMI`, `SYSTEM_IN_ALARM`, `ALARMS_PRESENT_TOTAL`,
`FAULT_SONALERT`, `Program:B_OVEN.Burner2_Fault_Latch`

### Cycle
- `OVEN_LOAD_CYCLE.HR_LOAD_TIME_LEFT_TO_MMI` REAL (14.0) - **hours** remaining
  (`.93` reports minutes; normalize on ingest)
- `OVEN_LOAD_CYCLE.HR_LOAD_TIME_TO_MMI` REAL - total load time
- `OVEN_LOAD_CYCLE.STEP_TIME_ELAPSED_TO_MMI` / `.STEP_TIME_TO_MMI` REAL
- `OVEN_LOAD_CYCLE.STEP_COMPLETE`, `.LOAD_TIMER_TIMED_OUT` BOOL
- `CURRENT_STEP`, `PREV_STEP`, `STEP_NUMBER` INT/DINT; `STEP_1_ACTIVE`..`STEP_5_ACTIVE` BOOL
- `RECIPE_COMPLETE` BOOL; `CURRENT_STEP_CONVERTED_RAMP_RATE` REAL (360.0)
- `OVEN_END_OF_CYCLE_TIMER.ACC` / `.PRE` DINT (0 / 960000 ms = 16 min)

### Fans
- `OVEN_EXHAUST.VFD_RUNNING`, `.VFD_AT_SPEED`, `.VFD_FAULTED` BOOL;
  `.ACTUAL_FREQ_TO_MMI` INT, `.AMPERAGE` REAL
- `RECIRC_FAN.MOTOR_RUNNING` BOOL (False), `.MOTOR_FAULT_BIT`, `.AIR_FLOW_SW_ON`

Note `RECIRC_FAN` is `OVEN_FAN` (no VFD) and `OVEN_EXHAUST` is `FREQ_DRIVE_MOTOR`
(VFD) - they do **not** share member names.

### Doors / parts
`ENTRANCE_DOOR_FINISH_OPEN`, `EXIT_DOOR_FINISH_OPEN`, `ENTRANCE_PHOTO_EYE_CLEAR`,
`EXIT_PHOTO_EYE_CLEAR`, `NO_PARTS_PRESENT`, `OPEN_DOORS_PARTWAY_FOR_COOLDOWN`

## Load temperature - use the AUX probes, not LOAD_TEMP_TO_MMI

**`OVEN_LOAD_CYCLE.LOAD_TEMP_TO_MMI` is a failed probe. Do not ingest it.**

It reads `2192`, which is exactly 1200 C expressed in F - the full-scale ceiling of a
type J thermocouple. The raw member `TCOUPLE_DATA_FR_CARD_ENG_UNITS` reads `21920.0`
(tenths), so the value is pinned at the top of range, not mis-scaled. The card's own
diagnostics do **not** catch it: `TCOUPLE_OPEN_CIRCUIT_FR_CARD`,
`TCOUPLE_UNDER_RANGE_FR_CARD` and `TOUPLE_OVER_RANGE_FR_CARD` all read False. The
Celsius conversions (`TCOUPLE_DATA_FR_CARD_DEG_C`, `LOAD_TEMP_TO_MMI_DEG_C`) read 0.

The real load instrumentation is `OVEN_AUX_THERMOCOUPLE` (`AUX_THERMOCOUPLES`) - eleven
probes, all reading plausibly with the oven cold and open (2026-08-26, operator-confirmed
as accurate):

`LEFT_LOAD_1`..`LEFT_LOAD_5` (95.9, 117.9, 87.2, 90.1, 82.2),
`RIGHT_LOAD_1`..`RIGHT_LOAD_5` (96.1, 112.5, 90.4, 85.3, 86.7), `TOP_LOAD` (104.3)

Ingest these as the load temperature (min/mean/max across probes), and treat any probe
at 2192.0 as invalid rather than as a reading.

## Auto/manual and doors - member names resolved

- `OVEN_AUTO_MANUAL` (`AUTO_MANUAL_START_STOP`): `.OVEN_AUTO_MODE_ENABLED` (False),
  `.OVEN_MANUAL_MODE_ENABLED` (True), `.OVEN_AUTO_OPERATION_ACTIVATED`,
  `.OVEN_AUTO_START_IN_PROGRESS`, `.OVEN_AUTO_STOP_IN_PROGRESS`, `.OVEN_PLC_OK`
- `OVEN_ENTRANCE_DOOR` / `OVEN_EXIT_DOOR` / `OVEN_DOORS` (`OVEN_DOOR`):
  `.RUNNING`, `.FULL_UP_LIMIT_SWITCH`, `.FULL_DOWN_LIMIT_SWITCH`, `.SAFETY_SW_ON`,
  `.OPEN_SELSW`, `.CLOSE_SELSW`, `.SAFETY_PULL_SWITCH_ON`

## Door polarity - CONFIRMED 2026-08-27

`OVEN_ENTRANCE_DOOR.FULL_DOWN_LIMIT_SWITCH` / `OVEN_EXIT_DOOR.FULL_DOWN_LIMIT_SWITCH` are
wired **normally-closed** - the raw bit reads inverted relative to what its name suggests.
Confirmed by two independent real-world checks, on different days, with the door in
different physical states, both correctly predicted by the same inversion:

| Date | Raw reads | Physical reality (operator-confirmed) | Literal reading | Inverted reading |
|---|---|---|---|---|
| 2026-08-26 | `True` | doors **open** | closed (wrong) | open (correct) |
| 2026-08-27 | `False` | doors **closed** (cycle running) | open (wrong) | closed (correct) |

The literal, non-inverted reading of the tag name is wrong both times; the inverted
reading is right both times. This is applied in `api/store_sqlite.py` /
`api/store_mongo.py` (`_door_closed()`) as derived `entrance_door_closed` /
`exit_door_closed` fields, surfaced in the dashboard's Doors tile. The raw
`entrance_door_down` / `exit_door_down` fields are never modified anywhere in
storage - only this one read-time derivation applies the correction. See
`collector/config.py`'s `CONFIRMED_ACTIVE_LOW`.

## Still open: other suspected active-low bits

**Do not encode any of these until confirmed** - guessing wrong either invents faults
that do not exist or hides real ones. Unlike the doors above, none of these have been
checked against an independently-known real-world state yet.

Read at 2026-08-26 ~08:40 with the oven cold and idle:

| Tag | Reads | Contradiction |
|---|---|---|
| `BURNER_1/2.BURNER_AT_HIGH_FIRE` | True | flame off, firing rate 0 |
| `BURNER_1/2.HIGH_LIMIT_OK` | False | no high-temperature fault set |
| `OVEN_AUTO_MANUAL.OVEN_PLC_OK` | False | the PLC is plainly fine - it answered the read |

These are consistent with the same normally-closed pattern the doors turned out to have,
but that is circumstantial - a matching pattern, not independent confirmation. Neither
`BURNER_AT_HIGH_FIRE` nor `HIGH_LIMIT_OK` is currently polled by the collector at all
(never added as canonical fields). `OVEN_PLC_OK` is polled (as `power_feed`) and remains
in `collector/config.py`'s `ACTIVE_LOW_SUSPECTS`, flagged but uncorrected.

## Burner 2 - possibly out of service

Three independent signals, read cold and idle:
`BURNER_2.LOW_GAS_PRESS_OK` = False (burner 1 = True),
`Program:B_OVEN.Burner2_Fault_Latch` = True, `USE_B2_TCOUPLE` = False.

If the active-low hypothesis above holds for `LOW_GAS_PRESS_OK`, this inverts to burner
**1** having the low-gas-pressure condition. The fault latch and the disabled
thermocouple still point at burner 2. Needs an operator answer.

## Other

`OVEN_SOAK_TIMER.ACC/.PRE` return `Path segment error` despite being listed as a
`TIMER` - likely program-scoped rather than controller-scoped.
Setpoints differ per burner (`BURNER_1` 250.0, `BURNER_2` 305.0) - confirm whether that
is an intentional two-zone profile.

## Mapping onto the unified two-oven schema

| Unified field | `.93` (proven) | `.91` |
|---|---|---|
| `zone1_temp` | `Z1_ACTUAL_TEMP` | `BURNER_1.CALIBRATED_TEMPERATURE` |
| `zone2_temp` | `Z2_ACTUAL_TEMP` | `BURNER_2.CALIBRATED_TEMPERATURE` |
| `setpoint` | `OVEN_TEMP_SETPOINT` | `BURNER_1.SET_POINT_FROM_MMI` (+ zone 2 separately) |
| `zone1_burner` | `ZONE_1_BURNER_MTR` | `BURNER_1.SCALED_CONTROL_VARIABLE` |
| `zone2_burner` | `ZONE_2_BURNER_MTR` | `BURNER_2.SCALED_CONTROL_VARIABLE` |
| `cycle_time_left_min` | `CYCLE_TOTAL_MINUTES_LEFT` | `OVEN_LOAD_CYCLE.HR_LOAD_TIME_LEFT_TO_MMI` x 60 |
| `exhaust_fan_active` | `EXHAUST_FAN` | `OVEN_EXHAUST.ACTUAL_FREQ_TO_MMI` |
| `auto_mode_selected` | `AUTOMATIC_MODE_SELECTED` | `OVEN_AUTO_MANUAL.OVEN_AUTO_MODE_ENABLED` |
| `combustion_fault_lock` | `CombustionFaultLock` | `BURNER_n.FLAME_FAILURE_FAULT` |
| `fault_lock` | `Fault_Lock` | `FAULT_ACTIVE` |
| `purge_fault` | `PURGE_FAULT` | `BURNER_n.PREMATURE_PURGE_TIME_FAULT` |
| `power_feed` | `POWER_FEED` | `OVEN_AUTO_MANUAL.OVEN_PLC_OK` (polarity unconfirmed) |
| `load_temp` | *(none)* | `OVEN_AUX_THERMOCOUPLE.*` - 11 probes |

`.91` has no direct analog for `SOAK_CYCLE_COMPLETE_COUNTER`; `STEP_NUMBER` /
`RECIPE_COMPLETE` are the closest equivalents.
