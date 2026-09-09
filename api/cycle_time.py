"""Cycle-time-remaining calculation, shared by both store backends.

Not duplicated per store_sqlite.py/store_mongo.py's usual convention (unlike
small one-line helpers like _door_closed) - this is real, non-trivial
business logic, and one implementation is worth keeping over two copies to
keep in sync.

Confirmed live 2026-08-27: neither oven has a PLC tag that live-counts
remaining cycle time THE WAY THE DASHBOARD ORIGINALLY WANTED IT - the small
oven's HR_LOAD_TIME_LEFT_TO_MMI is a frozen setpoint (read 40s apart during
an active cycle, temperature clearly changing, value never moved). The large
oven's CYCLE_TOTAL_MINUTES_LEFT genuinely counts down (350->349->349->349
over 60s), but confirmed live 2026-09-09 to be scoped to the CURRENT STEP
only, not the whole cycle - CYCLE_HOURS_LEFT/CYCLE_MINUTES_LEFT are just
that same value split into hours/minutes for display, and CYC_HR_LEFT_IN_MINUTES
is just CYCLE_HOURS_LEFT*60 - none of them are an independent cycle-wide figure.

So there are two related but distinct questions this module answers:

  step_remaining_min:  time left in the CURRENT step only.
  cycle_remaining_min: current step + every step still ahead of it.

Two different ways to get step_remaining_min, picked by
config.OVENS[...]["cycle_time_left_min_trusted"]:
  - Large oven: trust CYCLE_TOTAL_MINUTES_LEFT directly - it is proven
    accurate for the step it is scoped to, so there is no reason to
    recompute what the PLC already tracks correctly.
  - Small oven: computed from the active step's target temperature, ramp
    rate (seconds per degree F), and soak duration, plus the live actual
    temperature and a "ramp finished, soak started" timestamp. That
    timestamp is NOT tracked here or in collector memory - it is read fresh
    from step_events (collector/storage.py's record_step()) on every call,
    which is what makes this resilient to a collector restart: the anchor
    point already committed to the database is the one true a restart
    cannot lose, rather than something this calculation would otherwise
    have to reconstruct.

Extending to cycle_remaining_min is then identical for both ovens: full
ramp (from the previous step's target) + full soak for every step still
ahead, since none of them have begun yet.

CURRENT STEP INDEX: the small oven's PLC reports this directly
(current_step, 0-indexed). The large oven's does not expose one at all
(confirmed live 2026-09-09 - no ACTIVE/CURRENT-named tag exists) - it is
inferred instead by matching the live setpoint against each step's own
target temperature (OVEN_TEMP_SETPOINT tracks whichever step is presently
controlling the oven). That inference returns None, rather than a guess,
if zero or more than one step matches - which would happen if two steps in
the same recipe legitimately share a target temperature, a real possibility
this data cannot distinguish given the PLC exposes no step-sequence tag.
"""


def compute_remaining(snapshot, oven, state, steady_reached_ts, now):
    """Returns (step_remaining_min, cycle_remaining_min) - either may be
    None if there is not enough information to compute it.

    Gated on state == "RUNNING": an idle oven's recipe fields still hold
    whatever the LAST cycle used (confirmed live 2026-08-27 - the small
    oven read SP_TEMP=365 while idle and cooling from a completed load).
    Without this gate, an idle, cooling oven would compute a large bogus
    "remaining time" toward a cycle that is not actually happening.
    """
    if state != "RUNNING":
        return None, None

    count = snapshot.get("recipe_step_count")
    if count is None:
        return None, None

    step = snapshot.get("current_step")
    if step is None:
        step = _infer_current_step(snapshot, count)
    if step is None or not (0 <= step < count):
        return None, None

    if oven.get("cycle_time_left_min_trusted"):
        step_remaining_min = snapshot.get("cycle_time_left_min")
    else:
        step_remaining_min = _compute_step_remaining_min(snapshot, step, steady_reached_ts, now)
    if step_remaining_min is None:
        return None, None

    cycle_remaining_min = step_remaining_min + _future_steps_remaining_min(snapshot, step, count)
    return step_remaining_min, cycle_remaining_min


def _infer_current_step(snapshot, count):
    """Which 0-indexed step is presently controlling the oven, for an oven
    with no direct step-index tag - see the module docstring."""
    setpoint = snapshot.get("setpoint")
    if setpoint is None:
        return None
    matches = [i for i in range(count) if snapshot.get("recipe_step%d_temp" % i) == setpoint]
    return matches[0] if len(matches) == 1 else None


def _compute_step_remaining_min(snapshot, step, steady_reached_ts, now):
    """The current step's own remaining time, computed from scratch - used
    only when the native countdown tag cannot be trusted (the small oven)."""
    target_f = snapshot.get("recipe_step%d_temp" % step)
    ramp_rate_s_per_deg = snapshot.get("recipe_step%d_ramp_rate" % step)
    soak_hr = snapshot.get("recipe_step%d_soak_hr" % step)
    actual_f = snapshot.get("zone1_temp")
    at_steady = bool(snapshot.get("burner1_at_steady_temp"))
    if None in (target_f, ramp_rate_s_per_deg, soak_hr, actual_f):
        return None

    if not at_steady:
        # Still ramping: full soak still ahead once it arrives.
        ramp_remaining_s = abs(target_f - actual_f) * ramp_rate_s_per_deg
        soak_remaining_s = soak_hr * 3600.0
    else:
        ramp_remaining_s = 0.0
        elapsed_soak_s = max((now - steady_reached_ts).total_seconds(), 0.0) \
            if steady_reached_ts is not None else 0.0
        soak_remaining_s = max(soak_hr * 3600.0 - elapsed_soak_s, 0.0)

    return (ramp_remaining_s + soak_remaining_s) / 60.0


def _future_steps_remaining_min(snapshot, step, count):
    """Full ramp + full soak for every step still ahead of `step` (0-indexed,
    exclusive) - shared by both ovens, since neither has started any of
    those steps yet. Stops at the first step missing data rather than
    discarding whatever was already accumulated - a partial cycle estimate
    (e.g. steps 2-3 known, step 4 not yet populated) is more useful than
    none at all.
    """
    remaining_s = 0.0
    prev_target_f = snapshot.get("recipe_step%d_temp" % step)
    for i in range(step + 1, count):
        this_target_f = snapshot.get("recipe_step%d_temp" % i)
        this_ramp_rate = snapshot.get("recipe_step%d_ramp_rate" % i)
        this_soak_hr = snapshot.get("recipe_step%d_soak_hr" % i)
        if None in (this_target_f, this_ramp_rate, this_soak_hr):
            break
        remaining_s += abs(this_target_f - prev_target_f) * this_ramp_rate + this_soak_hr * 3600.0
        prev_target_f = this_target_f
    return remaining_s / 60.0
