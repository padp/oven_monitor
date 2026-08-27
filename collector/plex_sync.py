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
import time
from datetime import datetime, timedelta, timezone

from . import config, plex
from .storage import Storage

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


def _flatten(load, confirmed):
    """One furnace load + (if available) its first container's part info.

    Only ever resolves ONE container per load - the load can carry several,
    but the dashboard just needs "what's probably in this oven", not a full
    manifest. get_container failures (a real possibility - see plex.py's
    _post retry-then-raise) are caught here specifically, not by the caller,
    because a part-lookup failure should not discard an otherwise-good
    load/cycle result.
    """
    cycles = load.get("CyclesData") or []
    c0 = cycles[0] if cycles else {}
    containers = load.get("ContainersData") or []

    out = {
        "confirmed": confirmed,
        "furnace_load_no": load.get("FurnaceLoadNo"),
        "furnace_load_status": load.get("FurnaceLoadStatus"),
        "operation_code": load.get("OperationCode"),
        "temperature": c0.get("Temperature"),
        "actual_start_time": c0.get("ActualStartTime"),
        "actual_end_time": c0.get("ActualEndTime"),
        "serial_no": None, "job_no": None, "part_no": None,
        "part_name": None, "quantity": None,
    }
    if not containers:
        return out

    serial = containers[0].get("SerialNo")
    out["serial_no"] = serial
    out["job_no"] = containers[0].get("JobNo")
    if not serial:
        return out

    try:
        begin, end = _container_date_window(datetime.now(timezone.utc))
        rows = plex.get_container(serial, start_date=begin, end_date=end)
    except Exception as exc:
        print("plex_sync: get_container(%r) failed (keeping load info without part details): %s"
              % (serial, exc))
        return out

    if rows:
        out["part_no"] = rows[0].get("PartNo")
        out["part_name"] = rows[0].get("PartName")
        out["quantity"] = rows[0].get("Quantity")
    return out


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
