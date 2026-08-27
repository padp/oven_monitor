"""Cycle-time-remaining calculation, shared by both store backends.

Not duplicated per store_sqlite.py/store_mongo.py's usual convention (unlike
small one-line helpers like _door_closed) - this is real, non-trivial
business logic, and one implementation is worth keeping over two copies to
keep in sync.

Confirmed live 2026-08-27: neither oven has a PLC tag that live-counts
remaining cycle time - HR_LOAD_TIME_LEFT_TO_MMI and STEP_TIME_ELAPSED_TO_MMI
were both read 40s apart during an active cycle, with the actual
temperature clearly changing, and neither moved. This computes it instead,
the way the user described doing by hand before this collector existed:
from the active recipe's per-step target temperature, ramp rate (seconds
per degree F), and soak duration, plus the live actual temperature and a
"ramp finished, soak started" timestamp.

That timestamp is NOT tracked here or in collector memory - it is read
fresh from step_events (collector/storage.py's record_step()) on every
call, which is what makes this resilient to a collector restart: the
anchor point already committed to the database is the one true a restart
cannot lose, rather than something this calculation would otherwise have
to reconstruct.

Only wired up for the small oven so far. The large oven's equivalent
(S1-S4 tags, seconds-per-degree ramp math confirmed to exist via
USE_SECS_PER_DEG_RAMP_MATH) has not been validated against a live cycle -
the oven was idle throughout this investigation - so its canonical fields
are not populated yet and this returns None for it until they are.
"""


def compute_remaining_min(snapshot, state, steady_reached_ts, now):
    """Remaining minutes in the current recipe (current step + any steps
    after it), or None if there is no active step to compute from.

    Gated on state == "RUNNING": an idle oven's recipe fields still hold
    whatever the LAST cycle used (confirmed live 2026-08-27 - the small
    oven read SP_TEMP=365 while idle and cooling from a completed load).
    Without this gate, an idle, cooling oven would compute a large bogus
    "remaining time" toward a cycle that is not actually happening.
    """
    if state != "RUNNING":
        return None
    step = snapshot.get("current_step")
    count = snapshot.get("recipe_step_count")
    if step is None or count is None or not (0 <= step < count):
        return None

    def field(i, name):
        return snapshot.get("recipe_step%d_%s" % (i, name))

    target_f = field(step, "temp")
    ramp_rate_s_per_deg = field(step, "ramp_rate")
    soak_hr = field(step, "soak_hr")
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

    remaining_s = ramp_remaining_s + soak_remaining_s

    # Any steps after the current one: full ramp (from the PREVIOUS step's
    # target to this one's) plus full soak, since none of them have begun.
    prev_target_f = target_f
    for i in range(step + 1, count):
        this_target_f = field(i, "temp")
        this_ramp_rate = field(i, "ramp_rate")
        this_soak_hr = field(i, "soak_hr")
        if None in (this_target_f, this_ramp_rate, this_soak_hr):
            break
        remaining_s += abs(this_target_f - prev_target_f) * this_ramp_rate + this_soak_hr * 3600.0
        prev_target_f = this_target_f

    return remaining_s / 60.0
