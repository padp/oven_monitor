"""Periodic sync: what job is actually running in each oven, per Plex.

Separate from collector.py's 30s PLC poll loop deliberately - a Plex round
trip measured 2-20s live (see collector/plex.py), and Plex's own session
login/reauth can take longer still, so this runs on its own slower interval
rather than blocking or slowing down PLC polling. It writes into the SAME
oven_monitor.db collector.py writes to (a separate table, plex_loads), and
publisher.py forwards new rows from there to the cloud API exactly like
samples and state_events.

Must run on a machine with both LAN access to the shared
Extrusion DB/secret/ credentials and internet access to cloud.plex.com - the
poller host, not Render (which can reach neither).
"""
import re
import time
from datetime import datetime, timedelta, timezone

from . import config, plex
from .storage import Storage

_PROGRAM_NUMBER_RE = re.compile(r"#(\d+)")


def _program_number(operation_code):
    """Pull the program/recipe number out of OperationCode.

    Plex has no dedicated numeric field for this - only the free-text
    OperationCode ("Aging Prog #006 330_3.7"), which is what the PLC's own
    RECIPE_NUMBER-equivalent tags are cross-checked against on the
    dashboard. Some codes name two candidates ("Aging Prog #002 OR #018",
    observed live) - genuinely ambiguous from the string alone, so this
    returns None rather than guessing which one actually ran.
    """
    if not operation_code:
        return None
    matches = _PROGRAM_NUMBER_RE.findall(operation_code)
    return int(matches[0]) if len(matches) == 1 else None

# Plex latency (2-20s+ per call, sometimes more on a cold login) is much
# higher than the PLC's, and job/part context does not change nearly as
# often as temperature - there is no reason to poll this as fast as
# collector.py's 30s loop. 2 minutes keeps the dashboard's job context
# reasonably current without hammering Plex from every oven on every tick.
SYNC_INTERVAL_S = 120


def _date_window(now):
    """Yesterday 05:00 UTC through tomorrow 05:00 UTC.

    Matches the exact window shape from the original captured request (and
    the user's own note alongside it): the begin date should be one day
    before "today" to reliably catch a cycle that started the previous day,
    since FurnaceLoad/Search's BeginDate/EndDate has not been confirmed to
    filter strictly by real occurrence time (see get_current_load's
    docstring - an old, incompletely-logged record turned up in a 2-day
    window once already).
    """
    today = now.date()
    begin = datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) \
        .replace(hour=5)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) \
        .replace(hour=5)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return begin.strftime(fmt), end.strftime(fmt)


def _container_date_window(now):
    """Much wider than _date_window() - Container/Search needs it.

    Confirmed live 2026-08-27: a container whose load started TODAY still
    returned zero rows with a "yesterday" start date (the same window that
    works fine for FurnaceLoad/Search). Going back to the start of the
    PREVIOUS month reliably found it. This endpoint's date filter appears to
    key off something like the container's own creation date rather than
    the load's occurrence date - not confirmed against Plex's actual
    semantics, just empirically wide enough to stop missing real, current
    containers. Serial number is already a unique identifier by itself, so
    a wide window costs correctness nothing - it can only match more, never
    the wrong thing.
    """
    end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) \
        .replace(hour=5)
    begin = now - timedelta(days=90)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return begin.strftime(fmt), end.strftime(fmt)


def _resolve_container(container, begin, end):
    """One ContainersData entry -> {serial_no, job_no, part_no, part_name, quantity}.

    A per-container failure (a real possibility - see plex.py's _post
    retry-then-raise) returns what's already known (serial/job from the
    load itself) with part fields left None, rather than raising - one bad
    lookup must not discard the rest of the load's containers.
    """
    serial = container.get("SerialNo")
    out = {"serial_no": serial, "job_no": container.get("JobNo"),
           "part_no": None, "part_name": None, "quantity": None}
    if not serial:
        return out
    try:
        rows = plex.get_container(serial, start_date=begin, end_date=end)
    except Exception as exc:
        print("plex_sync: get_container(%r) failed (keeping serial/job without part details): %s"
              % (serial, exc))
        return out
    if rows:
        out["part_no"] = rows[0].get("PartNo")
        out["part_name"] = rows[0].get("PartName")
        out["quantity"] = rows[0].get("Quantity")
    return out


def _flatten(load, confirmed):
    """One furnace load, with every one of its containers resolved to a part.

    Measured live 2026-08-27: ~0.6-0.8s per container once the Plex session
    is warm, so even a load with 10-11 containers (both observed live) costs
    well under 10s total - comfortably inside the 120s sync interval for two
    ovens. Each container is resolved independently (see _resolve_container)
    so one bad lookup does not lose the rest.
    """
    cycles = load.get("CyclesData") or []
    c0 = cycles[0] if cycles else {}
    raw_containers = load.get("ContainersData") or []

    begin, end = _container_date_window(datetime.now(timezone.utc))
    containers = [_resolve_container(c, begin, end) for c in raw_containers]

    first = containers[0] if containers else {}
    return {
        "confirmed": confirmed,
        "furnace_load_no": load.get("FurnaceLoadNo"),
        "furnace_load_status": load.get("FurnaceLoadStatus"),
        "operation_code": load.get("OperationCode"),
        "program_number": _program_number(load.get("OperationCode")),
        "temperature": c0.get("Temperature"),
        "actual_start_time": c0.get("ActualStartTime"),
        "actual_end_time": c0.get("ActualEndTime"),
        # Kept as a compact "at a glance" summary (the dashboard's job-card
        # header) alongside the full per-serial breakdown in `containers` -
        # a load can mix more than one part (observed live: LH/RH shade
        # variants in the same load), so this is only ever a representative
        # example, not necessarily the whole story.
        "serial_no": first.get("serial_no"), "job_no": first.get("job_no"),
        "part_no": first.get("part_no"), "part_name": first.get("part_name"),
        "quantity": first.get("quantity"),
        "containers": containers,
    }


def sync_once(storage, ovens):
    for oven in ovens:
        wc = oven.get("plex_workcenter_key")
        if not wc:
            continue
        begin, end = _date_window(datetime.now(timezone.utc))
        try:
            load, confirmed = plex.get_current_load(wc, begin, end)
        except Exception as exc:
            print("plex_sync: %s: get_current_load failed (will retry next cycle): %s"
                  % (oven["name"], exc))
            continue
        if load is None:
            continue
        flat = _flatten(load, confirmed)
        storage.insert_plex_load(oven["id"], datetime.now(timezone.utc), flat)
        print("plex_sync: %s -> load %s (%s) part=%s qty=%s" % (
            oven["name"], flat["furnace_load_no"],
            "confirmed" if confirmed else "guess", flat["part_no"], flat["quantity"]))


def run():
    ovens = [o for o in config.enabled_ovens() if o.get("plex_workcenter_key")]
    if not ovens:
        print("No enabled ovens have a plex_workcenter_key configured - nothing to sync.")
        return

    storage = Storage()
    print("Syncing Plex job context for: %s (every %ds)" % (
        ", ".join(o["name"] for o in ovens), SYNC_INTERVAL_S))

    try:
        while True:
            try:
                sync_once(storage, ovens)
            except Exception as exc:
                # A bad cycle here must not take down a process that is
                # otherwise fine to just retry in a couple of minutes.
                print("plex_sync: sync_once failed (will retry): %s" % exc)
            time.sleep(SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping plex_sync.")
    finally:
        storage.close()
