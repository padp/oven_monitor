"""Oven state detection: RUNNING / IDLE / FAULT / UNKNOWN.

The logic here is carried forward from large_oven_status.py's
determine_state(), which has run against the large oven since October 2025
and encodes real operational knowledge rather than a guess at how an oven
ought to behave. Three things it gets right and are preserved exactly:

  1. Priority is RUNNING > FAULT > IDLE. An oven actively executing a
     cycle is RUNNING even if fault bits are set - a latched historical
     fault must not mask the fact that the oven is working right now.
  2. A fault is not just "a fault bit is on". It is also the *implicit*
     fault of a cycle with real time left on it while nothing is heating
     and temperatures have fallen - an unexpected mid-cycle stop.
  3. Cycle-complete is a small nonzero threshold, not == 0, because the
     counter lingers near zero at the end of a cycle.

One Detector instance per oven; it keeps per-oven memory (last cycle time,
last running timestamp) that must not be shared between ovens.

Deliberately NOT used in any decision here: the fields in
config.ACTIVE_LOW_SUSPECTS. Their polarity is unconfirmed (see
reference/oven_91_tag_map.md), and gating oven state on a bit that may
read backwards would either invent faults or hide real ones. They are
still recorded on every sample, which is what will eventually settle them.
"""
from . import config

RUNNING = "RUNNING"
IDLE = "IDLE"
FAULT = "FAULT"
UNKNOWN = "UNKNOWN"


class Detector:
    def __init__(self, oven):
        self.oven = oven
        self.oven_id = oven["id"]
        self.previous_cycle_time = None

    def evaluate(self, snapshot, load_stats=None):
        """Return (state, reason) for one canonical snapshot."""
        try:
            if self._is_running(snapshot):
                return RUNNING, self._running_reason(snapshot)
            fault_reason = self._fault_reason(snapshot)
            if fault_reason:
                return FAULT, fault_reason
            if self._is_idle(snapshot):
                return IDLE, self._idle_reason(snapshot)
            return UNKNOWN, "Unable to determine state from available data"
        except Exception as exc:
            # A detector bug must not take down the poll loop - the sample
            # itself is still worth recording.
            return UNKNOWN, "Error in state determination: %s" % exc

    # --- RUNNING ------------------------------------------------------

    def _is_running(self, s):
        z1 = num(s.get("zone1_temp"))
        z2 = num(s.get("zone2_temp"))

        temps_at_process = z1 > config.PROCESS_TEMP_F or z2 > config.PROCESS_TEMP_F
        equipment_active = self._equipment_active(s)

        self.previous_cycle_time = num(s.get("cycle_time_left_min"))

        if self.oven.get("cycle_active_field"):
            # cycle_active_field (BEGIN_LOAD_TIME_TIMER on the small oven) is
            # confirmed NOT reliable as a sustained "cycle in progress" gate -
            # read live 2026-08-27 as False while both burners were actively
            # firing (MAIN_FLAME_ON true, firing rate >2000) partway through a
            # real cycle, which produced a false UNKNOWN instead of RUNNING.
            # equipment_active is direct, real-time evidence of active
            # operation (flame/firing/PID bits, not a timer) - trust it on its
            # own. Temperature alone still requires the timer's corroboration,
            # since elevated temp with no active equipment could just be a
            # recently-finished cycle cooling down rather than a genuinely new
            # one starting.
            return equipment_active or (temps_at_process and self._cycle_active(s))

        return self._cycle_active(s) and (temps_at_process or equipment_active)

    def _cycle_active(self, s):
        """Is a load cycle actually running?

        The large oven has no dedicated bit for this, but its
        CYCLE_TOTAL_MINUTES_LEFT genuinely counts down to 0 when idle, so
        "time remaining > 0" is a sound proxy - and is exactly what ~10
        months of validated history was produced with.

        The small oven's equivalent tag is a setpoint that sits at the
        operator-entered load time whether or not anything is running, so
        it declares an explicit bit instead. Inferring from time remaining
        there produced a confident FAULT on a cold, idle oven.
        """
        field = self.oven.get("cycle_active_field")
        if field:
            return boolean(s.get(field))
        return num(s.get("cycle_time_left_min")) > 0

    def _equipment_active(self, s):
        """Any sign the oven is actually doing work.

        The large oven only exposes burner motor output and an exhaust
        flag. The small oven also reports flame and PID state directly,
        which is a stronger signal, so it is included where present -
        absent fields read as None and simply contribute nothing.
        """
        if num(s.get("zone1_burner")) > 0 or num(s.get("zone2_burner")) > 0:
            return True
        if num(s.get("exhaust_fan_active")) > 0:
            return True
        for field in ("flame_on", "burner1_flame_on", "burner2_flame_on", "pid_active"):
            if boolean(s.get(field)):
                return True
        return False

    def _running_reason(self, s):
        cycle_left = num(s.get("cycle_time_left_min"))
        z1 = num(s.get("zone1_temp"))
        z2 = num(s.get("zone2_temp"))
        return "Oven running cycle - %d minutes remaining, Temps: Z1=%d F Z2=%d F" % (
            round(cycle_left), round(z1), round(z2),
        )

    # --- FAULT --------------------------------------------------------

    def _fault_reason(self, s):
        """Explicit fault bits first, then the implicit mid-cycle stop."""
        for field, label in (
            ("combustion_fault_lock", "Combustion fault lock active"),
            ("fault_lock", "Fault lock active"),
            ("purge_fault", "Purge fault active"),
            ("burner2_flame_failure", "Burner 2 flame failure"),
            ("burner1_high_temp_fault", "Burner 1 high temperature fault"),
            ("burner2_high_temp_fault", "Burner 2 high temperature fault"),
            ("burner1_open_tc_fault", "Burner 1 open thermocouple"),
            ("burner2_open_tc_fault", "Burner 2 open thermocouple"),
        ):
            if boolean(s.get(field)):
                return "FAULT: %s" % label

        if not self.oven.get("implicit_stall_fault", True):
            return None

        cycle_left = num(s.get("cycle_time_left_min"))
        z1 = num(s.get("zone1_temp"))
        z2 = num(s.get("zone2_temp"))
        cycle_should_be_active = cycle_left > config.CYCLE_STALLED_MIN
        temps_below_process = z1 < config.COOL_TEMP_F and z2 < config.COOL_TEMP_F
        if cycle_should_be_active and not self._equipment_active(s) and temps_below_process:
            return (
                "FAULT: Cycle stopped unexpectedly - %d minutes left, no heating activity, "
                "Temps: Z1=%d F Z2=%d F" % (round(cycle_left), round(z1), round(z2))
            )
        return None

    # --- IDLE ---------------------------------------------------------

    def _is_idle(self, s):
        burners_idle = num(s.get("zone1_burner")) == 0 and num(s.get("zone2_burner")) == 0
        if self.oven.get("cycle_active_field"):
            return not self._cycle_active(s) and burners_idle
        # Large oven: a small nonzero threshold rather than == 0, because
        # the counter lingers just above zero as a cycle finishes.
        no_cycle = num(s.get("cycle_time_left_min")) <= config.CYCLE_COMPLETE_MIN
        return no_cycle and burners_idle

    def _idle_reason(self, s):
        z1 = num(s.get("zone1_temp"))
        z2 = num(s.get("zone2_temp"))
        return "Oven idle - Temps: Z1=%d F Z2=%d F" % (round(z1), round(z2))


def num(value, default=0.0):
    """Coerce a PLC value to a float.

    Values can arrive as None (tag read failed), as bools, or - if a tag
    turns out to be structured rather than scalar - as a bytes blob. None
    of those should raise mid-poll.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def boolean(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return default
