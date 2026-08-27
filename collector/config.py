"""Central configuration for the oven collector.

Two ovens, two completely different tag-naming conventions (see
reference/oven_91_tag_map.md). Rather than teach the detector and storage
about both, each oven declares a mapping from its own PLC tag names to a
shared set of CANONICAL field names, so everything downstream of
PlcClient works in canonical terms and neither oven is the special case.

Add newly discovered tags to the relevant oven's `tags` dict here rather
than scattering tag-name strings through the detection/storage code.
"""
import os

# Matches the legacy large-oven poller's 30s interval. Deliberately the
# same: the large oven has ~10 months of history at 30s, and keeping the
# small oven on the same cadence means the two datasets are directly
# comparable rather than needing resampling to line up.
POLL_INTERVAL_S = 30.0

# --- Canonical fields -------------------------------------------------
# The vocabulary the detector, storage and API all speak. Both ovens map
# into this; fields an oven cannot supply are simply absent from its map
# and read back as None.
#
#   zone1_temp, zone2_temp        F, actual
#   setpoint, setpoint2           F, commanded (small oven is per-burner)
#   zone1_burner, zone2_burner    firing rate, 0-100ish
#   cycle_time_left_min           minutes remaining in the load cycle
#   exhaust_fan_active            exhaust running/speed
#   auto_mode_selected            BOOL
#   manual_mode_selected          BOOL
#   combustion_fault_lock         BOOL
#   fault_lock                    BOOL
#   purge_fault                   BOOL
#   power_feed                    BOOL

# --- Large oven (10.4.20.93) ------------------------------------------
# Flat scalar tags, taken verbatim from large_oven_status.py's
# monitoring_tags. Proven correct by ~10 months of production logging -
# do not change these without a reason grounded in real data.
LARGE_OVEN_TAGS = {
    "Z1_ACTUAL_TEMP": "zone1_temp",
    "Z2_ACTUAL_TEMP": "zone2_temp",
    "OVEN_TEMP_SETPOINT": "setpoint",
    # The active program/recipe number - confirmed live 2026-08-27 reading
    # 2 while running "Aging Prog #002 365_6hrs" per Plex, matching exactly.
    "RECIPE_NUMBER": "recipe_number",
    "ZONE_1_BURNER_MTR": "zone1_burner",
    "ZONE_2_BURNER_MTR": "zone2_burner",
    "CYCLE_TOTAL_MINUTES_LEFT": "cycle_time_left_min",
    "SOAK_CYCLE_COMPLETE_COUNTER": "soak_cycle_counter",
    "AUTOMATIC_MODE_SELECTED": "auto_mode_selected",
    "MANUAL_MODE_SELECTED": "manual_mode_selected",
    "CombustionFaultLock": "combustion_fault_lock",
    "Fault_Lock": "fault_lock",
    "PURGE_FAULT": "purge_fault",
    "z1_safeguard_relay": "z1_safeguard_relay",
    "z2_safeguard_relay": "z2_safeguard_relay",
    "EXHAUST_FAN": "exhaust_fan_active",
    "POWER_FEED": "power_feed",
}

# --- Small oven (10.4.20.91) ------------------------------------------
# UDT-heavy. BURNER_1/BURNER_2 are OVEN_GAS_TRAIN instances; the useful
# scalars live on their members. CALIBRATED_TEMPERATURE is preferred over
# ACTUAL_TEMPERATURE_WIRE_1 because it has BURNER_n_TC_CALIBRATION
# already applied; WIRE_2 is unused on both burners (reads 0.0).
SMALL_OVEN_TAGS = {
    "BURNER_1.CALIBRATED_TEMPERATURE": "zone1_temp",
    "BURNER_2.CALIBRATED_TEMPERATURE": "zone2_temp",
    "BURNER_1.SET_POINT_FROM_MMI": "setpoint",
    "BURNER_2.SET_POINT_FROM_MMI": "setpoint2",
    "BURNER_1.SCALED_CONTROL_VARIABLE": "zone1_burner",
    "BURNER_2.SCALED_CONTROL_VARIABLE": "zone2_burner",
    "OVEN_LOAD_CYCLE.HR_LOAD_TIME_LEFT_TO_MMI": "cycle_time_left_min",
    "OVEN_EXHAUST.ACTUAL_FREQ_TO_MMI": "exhaust_fan_active",

    # Whether a load cycle is actually RUNNING. This is not inferrable
    # from cycle_time_left_min the way it is on the large oven, and
    # assuming otherwise produced a false FAULT on the very first live
    # poll. HR_LOAD_TIME_LEFT_TO_MMI is a *setpoint*, not a countdown:
    # with the oven cold and idle it read 14.0 hours purely because
    # TENTHS_HR_LOAD_TIME_FROM_MMI (the operator-entered load time) is
    # 140 - the two are identical until a cycle actually starts. The
    # large oven's CYCLE_TOTAL_MINUTES_LEFT does drop to 0 when idle,
    # hence the difference.
    "OVEN_LOAD_CYCLE.BEGIN_LOAD_TIME_TIMER": "cycle_active",
    "OVEN_LOAD_CYCLE.BEGIN_OVERALL_LOAD_TIME_TIMER": "overall_cycle_active",
    "OVEN_LOAD_CYCLE.HR_LOAD_TIME_COUNTER_TO_MMI": "cycle_elapsed_hr",
    "OVEN_LOAD_CYCLE.LOAD_TIMER_TIMED_OUT": "load_timer_timed_out",
    "OVEN_LOAD_CYCLE.TENTHS_HR_LOAD_TIME_FROM_MMI": "load_time_setpoint_tenths_hr",
    "OVEN_AUTO_MANUAL.OVEN_AUTO_OPERATION_ACTIVATED": "auto_operation_active",
    "OVEN_AUTO_MANUAL.OVEN_AUTO_MODE_ENABLED": "auto_mode_selected",
    "OVEN_AUTO_MANUAL.OVEN_MANUAL_MODE_ENABLED": "manual_mode_selected",
    "OVEN_AUTO_MANUAL.OVEN_PLC_OK": "power_feed",

    # The active program/recipe number - confirmed live 2026-08-27 reading
    # 6 while running "Aging Prog #006 330_3.7" per Plex, matching exactly.
    # Recipe_Number_Running specifically (not .Recipe_Number, which reads
    # the same right now but is presumably the one being EDITED/selected
    # rather than what's actually executing - Running is the safer choice).
    "OVEN_RECIPE_CONTROL.Recipe_Number_Running": "recipe_number",

    # Flame / firing state - richer than the large oven exposes.
    "BURNER_1.MAIN_FLAME_ON": "burner1_flame_on",
    "BURNER_2.MAIN_FLAME_ON": "burner2_flame_on",
    "BURNER_1.PILOT_ON": "burner1_pilot_on",
    "BURNER_2.PILOT_ON": "burner2_pilot_on",
    "BURNER_1.PURGING": "burner1_purging",
    "BURNER_2.PURGING": "burner2_purging",
    "FLAME_ON": "flame_on",
    "PID_ACTIVE": "pid_active",

    # Faults. The small oven has no single "combustion fault lock" bit;
    # per-burner flame failure is the closest equivalent, and the
    # top-level rollups cover the rest.
    "BURNER_1.FLAME_FAILURE_FAULT": "combustion_fault_lock",
    "BURNER_2.FLAME_FAILURE_FAULT": "burner2_flame_failure",
    "BURNER_1.HIGH_TEMPERATURE_FAULT": "burner1_high_temp_fault",
    "BURNER_2.HIGH_TEMPERATURE_FAULT": "burner2_high_temp_fault",
    "BURNER_1.OPEN_THERMOCOUPLE_FAULT": "burner1_open_tc_fault",
    "BURNER_2.OPEN_THERMOCOUPLE_FAULT": "burner2_open_tc_fault",
    "FAULT_ACTIVE": "fault_lock",
    "SYSTEM_IN_ALARM": "system_in_alarm",
    "ALARMS_PRESENT_TOTAL": "alarms_present_total",

    # Cycle / step progress.
    #
    # STEP_TIME_ELAPSED_TO_MMI / STEP_TIME_TO_MMI and cycle_time_left_min
    # above are NOT live countdowns despite the names - confirmed live
    # 2026-08-27 by reading them 40s apart during an active cycle while the
    # actual temperature was clearly changing: they never moved. The real
    # remaining-time calculation (see api/store_*.py's
    # compute_cycle_remaining()) is built from the recipe fields below plus
    # the live actual temperature instead, matching what the user
    # described having to do by hand before this collector existed.
    "OVEN_LOAD_CYCLE.STEP_TIME_ELAPSED_TO_MMI": "step_time_elapsed",
    "OVEN_LOAD_CYCLE.STEP_TIME_TO_MMI": "step_time_total",
    "OVEN_LOAD_CYCLE.STEP_COMPLETE": "step_complete",
    "STEP_NUMBER": "step_number",
    "CURRENT_STEP": "current_step",
    "RECIPE_COMPLETE": "recipe_complete",

    # The live recipe actually driving the oven right now (as opposed to
    # OVEN_RECIPE_COMPARE/_VIEW, which are for editing/comparing, not
    # execution). SP_TEMP/RAMP_RATE/SOAK_TIME are a 5-element array
    # (indices 0-4, matching STEP_1..STEP_5) - CURRENT_STEP above is
    # confirmed 0-indexed against this same array (read live alongside
    # STEP_NUMBER=1, the 1-indexed equivalent). SOAK_TIME is in HOURS.
    # SP_TEMP is polled by CALIBRATED_TEMPERATURE for zone1_temp already;
    # this is the recipe's TARGET, a separate live-confirmed real value
    # (365, matching the live PLC setpoint exactly).
    "OVEN_RECIPE_RUN.SIZE": "recipe_step_count",
    "OVEN_RECIPE_RUN.SP_TEMP[0]": "recipe_step0_temp",
    "OVEN_RECIPE_RUN.RAMP_RATE[0]": "recipe_step0_ramp_rate",
    "OVEN_RECIPE_RUN.SOAK_TIME[0]": "recipe_step0_soak_hr",
    "OVEN_RECIPE_RUN.SP_TEMP[1]": "recipe_step1_temp",
    "OVEN_RECIPE_RUN.RAMP_RATE[1]": "recipe_step1_ramp_rate",
    "OVEN_RECIPE_RUN.SOAK_TIME[1]": "recipe_step1_soak_hr",
    "OVEN_RECIPE_RUN.SP_TEMP[2]": "recipe_step2_temp",
    "OVEN_RECIPE_RUN.RAMP_RATE[2]": "recipe_step2_ramp_rate",
    "OVEN_RECIPE_RUN.SOAK_TIME[2]": "recipe_step2_soak_hr",
    "OVEN_RECIPE_RUN.SP_TEMP[3]": "recipe_step3_temp",
    "OVEN_RECIPE_RUN.RAMP_RATE[3]": "recipe_step3_ramp_rate",
    "OVEN_RECIPE_RUN.SOAK_TIME[3]": "recipe_step3_soak_hr",
    "OVEN_RECIPE_RUN.SP_TEMP[4]": "recipe_step4_temp",
    "OVEN_RECIPE_RUN.RAMP_RATE[4]": "recipe_step4_ramp_rate",
    "OVEN_RECIPE_RUN.SOAK_TIME[4]": "recipe_step4_soak_hr",

    # Ramp-complete / soak-begun signal, per burner. Zone 1 (burner 1) is
    # the trusted thermocouple (USE_B1_TCOUPLE=True, USE_B2_TCOUPLE=False
    # confirmed live) so burner1_at_steady_temp is the authoritative one
    # the remaining-time calculation uses.
    "BURNER_1_AT_STEADY_TEMP": "burner1_at_steady_temp",
    "BURNER_2_AT_STEADY_TEMP": "burner2_at_steady_temp",

    # Doors - polarity unconfirmed, see ACTIVE_LOW_SUSPECTS below.
    "OVEN_ENTRANCE_DOOR.FULL_DOWN_LIMIT_SWITCH": "entrance_door_down",
    "OVEN_EXIT_DOOR.FULL_DOWN_LIMIT_SWITCH": "exit_door_down",
    "OVEN_ENTRANCE_DOOR.RUNNING": "entrance_door_moving",
    "OVEN_EXIT_DOOR.RUNNING": "exit_door_moving",
}

# The small oven's real load instrumentation: eleven probes on the load
# itself, which the large oven has no equivalent of. Stored as aggregates
# plus the full raw vector - see storage.py.
#
# NOTE the deliberate omission of OVEN_LOAD_CYCLE.LOAD_TEMP_TO_MMI. That
# probe is failed: it reads 2192 F, which is exactly 1200 C - the
# full-scale ceiling of a type J thermocouple - while these eleven read
# 82-118 F and the operator confirmed ~100 F is correct. The card's own
# open-circuit and over-range bits do not catch it, so it must be
# excluded here rather than relied on to self-report.
SMALL_OVEN_LOAD_TC_TAGS = (
    ["OVEN_AUX_THERMOCOUPLE.LEFT_LOAD_%d" % i for i in range(1, 6)]
    + ["OVEN_AUX_THERMOCOUPLE.RIGHT_LOAD_%d" % i for i in range(1, 6)]
    + ["OVEN_AUX_THERMOCOUPLE.TOP_LOAD"]
)

# A thermocouple pinned at type J full scale is reporting "no signal",
# not 2192 F. Treat any probe at this value as missing.
INVALID_TC_F = 2192.0

# --- Unit normalization -----------------------------------------------
# Multiplied into the canonical value on ingest. The small oven reports
# load time remaining in HOURS while the large oven reports MINUTES, and
# cycle_time_left_min is defined in minutes.
SMALL_OVEN_SCALES = {"cycle_time_left_min": 60.0}

# --- Bit polarity -----------------------------------------------------
# The collector still stores every bit RAW, exactly as the PLC reports it,
# regardless of what's confirmed below - interpretation happens at read
# time (api/store_*.py), never in storage, so a correction here can never
# retroactively change what's on disk.

# CONFIRMED 2026-08-27, via two independent real-world checks on different
# days with the door in different physical states, both correctly predicted
# by the SAME inversion:
#   2026-08-26: raw True,  doors physically OPEN   (operator-confirmed)
#   2026-08-27: raw False, doors physically CLOSED (cycle running)
# i.e. FULL_DOWN_LIMIT_SWITCH is wired normally-closed - invert the raw
# value to get the true door state. Applied in api/store_*.py as derived
# entrance_door_closed / exit_door_closed fields; the raw
# entrance_door_down / exit_door_down fields are unaffected and still show
# exactly what the PLC reports.
CONFIRMED_ACTIVE_LOW = {
    "entrance_door_down",
    "exit_door_down",
}

# Still genuinely unresolved. OVEN_PLC_OK reads False while the PLC is
# plainly fine (it answered the read), which is consistent with the same
# normally-closed pattern as the doors, but that is circumstantial, not
# independently confirmed against a real-world state the way the doors now
# are - so it is not corrected anywhere, only flagged.
ACTIVE_LOW_SUSPECTS = {
    "power_feed",
}

# --- Oven registry ----------------------------------------------------
# Every stored row carries oven_id, so adding the large oven later is a
# config change (enabled: True) rather than a schema change.
OVENS = {
    "small": {
        "id": "small",
        "name": "Small Oven",
        "ip": "10.4.20.91",
        # Enabled alone for now, deliberately: the small oven is the one
        # whose behaviour still needs establishing, and its bit polarity
        # can only be settled by watching real cycles. The large oven is
        # meanwhile still covered by the legacy poller, so nothing is
        # unmonitored while this runs.
        "enabled": True,
        "tags": SMALL_OVEN_TAGS,
        "load_tc_tags": SMALL_OVEN_LOAD_TC_TAGS,
        "scales": SMALL_OVEN_SCALES,
        # Use the load timer's own run bit rather than inferring a
        # running cycle from time-remaining - see the note on
        # cycle_active in SMALL_OVEN_TAGS.
        "cycle_active_field": "cycle_active",
        # OFF until a real cycle has been observed end to end. The
        # implicit "cycle stopped unexpectedly" rule only makes sense if
        # the cycle signal is trustworthy; on the large oven it is,
        # backed by ~10 months of logging. Here it is one live reading
        # old. A false FAULT is worse than a missing one - it poisons
        # uptime numbers and trains people to ignore the alert - so this
        # oven reports the cycle it can see and stays quiet about
        # inferred stalls until the data earns it.
        "implicit_stall_fault": False,
        # Plex's WorkcenterKey for this oven - see collector/plex.py's
        # WORKCENTER_SMALL_OVEN. Used by collector/plex_sync.py to look up
        # what job is actually running, alongside the PLC telemetry.
        "plex_workcenter_key": "58085",
        # False: cycle_time_left_min is HR_LOAD_TIME_LEFT_TO_MMI here, which
        # is a frozen setpoint, not a countdown - confirmed live 2026-08-27,
        # read 40s apart during an active cycle with no change. Use the
        # recipe-based calculation instead (api/cycle_time.py).
        "cycle_time_left_min_trusted": False,
    },
    "large": {
        "id": "large",
        "name": "Large Oven",
        "ip": "10.4.20.93",
        # Enabled 2026-08-26, alongside the small oven's pattern-establishing
        # run. large_oven_status.py keeps running too - read-only pollers
        # coexist fine on EtherNet/IP - as a redundant, independently-proven
        # data source while this collector's coverage of .93 is new.
        "enabled": True,
        "tags": LARGE_OVEN_TAGS,
        "load_tc_tags": [],
        "scales": {},
        # None: fall back to cycle_time_left_min > 0, which is what
        # large_oven_status.py has always used and what replaying 691,366
        # historical snapshots through this detector reproduces exactly.
        "cycle_active_field": None,
        "implicit_stall_fault": True,
        "plex_workcenter_key": "58084",
        # True: CYCLE_TOTAL_MINUTES_LEFT genuinely counts down here -
        # confirmed live 2026-08-27 during a real cycle (350 -> 349 -> 349
        # -> 349 over 60s), and it's what large_oven_status.py has trusted
        # for ~10 months. Unlike the small oven, no recipe-based
        # calculation is needed - this oven already has a working native
        # countdown. (Its own recipe parameters exist under a different
        # naming scheme, S1_CYC_RAMP_SPT and friends, but have not been
        # validated - the suspicious-default values seen even during this
        # same real cycle suggest they may not be what actually drives it.
        # Moot for now: this field already works.)
        "cycle_time_left_min_trusted": True,
    },
}


def enabled_ovens():
    return [o for o in OVENS.values() if o["enabled"]]


# --- State detection tuning -------------------------------------------
# Carried forward from large_oven_status.py's determine_state(), which
# these thresholds come from - they encode ~10 months of validated
# operational behaviour on the large oven. Kept as named constants here
# instead of magic numbers in the detector so the small oven's values can
# diverge later if its real cycles turn out to differ.
PROCESS_TEMP_F = 200.0        # at/above this, the oven is at process heat
COOL_TEMP_F = 300.0           # below this mid-cycle suggests an unexpected stop
CYCLE_STALLED_MIN = 5.0       # minutes remaining that *should* mean activity
CYCLE_COMPLETE_MIN = 2.0      # at/below this, treat the cycle as finished

# --- Connection / reliability -----------------------------------------
MAX_CONSECUTIVE_ERRORS = 10
ERROR_BACKOFF_S = 2

# --- Storage ----------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SQLite's locking depends on file-lock primitives that SMB only partially
# emulates, which produces real "database is locked" / disk I/O errors
# under concurrent access from a network share - and this project root IS
# a network share. OVEN_DB_DIR lets the deployment put the DB on local
# disk while defaulting to the project-relative location.
_DB_DIR = os.environ.get("OVEN_DB_DIR", os.path.join(_PROJECT_ROOT, "db"))
DB_PATH = os.path.join(_DB_DIR, "oven_monitor.db")

RAW_BUFFER_RETENTION_HOURS = 48
RAW_BUFFER_PRUNE_INTERVAL_S = 300
