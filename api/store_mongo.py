"""Read-side access to collected oven data, backed by MongoDB Atlas.

The cloud counterpart to store_sqlite. Same four functions, same return
shapes - see api/store.py for how one is chosen.

Documents arrive via /ingest exactly as the collector wrote them to
SQLite, plus a source_id. That includes snapshot_json and load_temps_json
staying JSON *strings* rather than being expanded into nested documents.
Deliberate: it keeps the two stores symmetrical, keeps the wire format a
straight table dump, and means a new canonical field needs no schema
change anywhere in the pipeline.
"""
import json
from datetime import datetime, timedelta, timezone

from collector import config as collector_config
from . import cycle_time
from .db import get_db

# See the identical constant in store_sqlite.py for the full reasoning: a
# poll-interval-based allowance plus a flat 5-minute buffer for the poller
# host's observed clock drift (its system clock runs a couple of minutes
# behind true time, and syncing it is managed by org policy, not something
# fixable locally).
CLOCK_DRIFT_ALLOWANCE_S = 5 * 60
STALE_AFTER_S = collector_config.POLL_INTERVAL_S * 2.5 + CLOCK_DRIFT_ALLOWANCE_S


def _parse_ts(value):
    """Parse a stored ts string, always returning a timezone-AWARE datetime.

    New rows are UTC-aware (collector writes datetime.now(timezone.utc)).
    A handful of early rows from before that fix are naive local time - a
    naive value here is treated as UTC rather than left ambiguous, so it
    can never crash a subtraction against an aware "now". This is a
    one-time approximation for that first batch of data, not a general
    guarantee those specific old rows' ages are exactly right.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latest(db, oven_id):
    return db.samples.find_one(
        {"oven_id": oven_id}, sort=[("ts", -1)], projection={"_id": False}
    )


def ovens():
    db = get_db()
    out = []
    for oven in collector_config.OVENS.values():
        row = _latest(db, oven["id"])
        out.append({
            "id": oven["id"],
            "name": oven["name"],
            "ip": oven["ip"],
            "enabled": oven["enabled"],
            "last_sample_ts": row["ts"] if row else None,
            "state": row.get("state") if row else None,
        })
    return out


def current(oven_id):
    oven = collector_config.OVENS.get(oven_id)
    if oven is None:
        return None
    row = _latest(get_db(), oven_id)
    oven_meta = {"id": oven["id"], "name": oven["name"], "ip": oven["ip"],
                 "enabled": oven["enabled"]}
    if row is None:
        return {"oven": oven_meta, "sample": None, "stale": True, "age_s": None,
                "fields": [], "load_temps": []}

    snapshot = json.loads(row["snapshot_json"]) if row.get("snapshot_json") else {}
    load_temps = json.loads(row["load_temps_json"]) if row.get("load_temps_json") else {}

    field_to_tag = {field: tag for tag, field in oven["tags"].items()}
    fields = [{
        "field": field,
        "tag": field_to_tag.get(field),
        "value": value,
        "type": _value_type(value),
        "polarity_unconfirmed": field in collector_config.ACTIVE_LOW_SUSPECTS,
    } for field, value in snapshot.items()]
    fields.sort(key=lambda f: f["field"])

    ts = _parse_ts(row["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None

    return {
        "oven": oven_meta,
        "sample": {
            **{k: row.get(k) for k in (
                "ts", "state", "reason", "zone1_temp", "zone2_temp", "setpoint",
                "zone1_burner", "zone2_burner", "cycle_time_left_min",
                "exhaust_fan_active", "load_temp_min", "load_temp_mean",
                "load_temp_max", "load_temp_valid_count")},
            "entrance_door_closed": _door_closed(snapshot, "entrance"),
            "exit_door_closed": _door_closed(snapshot, "exit"),
            "cycle_time_remaining_computed_min": _best_remaining_min(
                oven, row, snapshot, oven_id, datetime.now(timezone.utc)),
        },
        "stale": age is None or age > STALE_AFTER_S,
        "age_s": age,
        "poll_interval_s": collector_config.POLL_INTERVAL_S,
        "fields": fields,
        "load_temps": [{"probe": name, "value": value, "valid": _probe_valid(value)}
                       for name, value in load_temps.items()],
    }


def history(oven_id, hours=6, limit=2000):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    keep = ("ts", "state", "zone1_temp", "zone2_temp", "setpoint", "zone1_burner",
            "zone2_burner", "cycle_time_left_min", "exhaust_fan_active",
            "load_temp_mean", "load_temp_min", "load_temp_max")
    rows = list(get_db().samples.find(
        {"oven_id": oven_id, "ts": {"$gte": cutoff}},
        projection={"_id": False, **{k: True for k in keep}},
    ).sort("ts", -1).limit(limit))
    return [{k: r.get(k) for k in keep} for r in reversed(rows)]


def states(oven_id, hours=24):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = list(get_db().state_events.find(
        {"oven_id": oven_id,
         "$or": [{"ts_end": None}, {"ts_end": {"$gte": cutoff}}]},
        projection={"_id": False},
    ).sort("ts_start", 1))

    now = datetime.now(timezone.utc)
    segments = []
    totals = {}
    for r in rows:
        start = _parse_ts(r.get("ts_start"))
        end = _parse_ts(r.get("ts_end")) if r.get("ts_end") else now
        if start is None or end is None:
            continue
        duration = max((end - start).total_seconds(), 0.0)
        segments.append({
            "ts_start": r.get("ts_start"),
            "ts_end": r.get("ts_end"),
            "state": r.get("state"),
            "reason": r.get("reason"),
            "duration_s": duration,
            "open": r.get("ts_end") is None,
        })
        totals[r.get("state")] = totals.get(r.get("state"), 0.0) + duration

    observed = sum(totals.values())
    return {
        "segments": segments,
        "totals_s": totals,
        "observed_s": observed,
        "share_pct": {k: (100.0 * v / observed if observed else 0.0)
                      for k, v in totals.items()},
    }


# See the identical constant in store_sqlite.py: plex_sync.py runs every
# 120s and a cycle can take ~20s+ (Plex latency, occasional fresh login),
# much slower than the PLC's 30s loop, so this needs its own allowance.
PLEX_STALE_AFTER_S = 10 * 60


def current_plex_load(oven_id):
    """Latest Plex job-context lookup for one oven, or None if plex_sync.py
    has never run (or never found a plex_workcenter_key configured) for it.
    """
    row = get_db().plex_loads.find_one(
        {"oven_id": oven_id}, sort=[("ts", -1)], projection={"_id": False}
    )
    if row is None:
        return None

    ts = _parse_ts(row.get("ts"))
    age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None

    keep = ("ts", "confirmed", "furnace_load_no", "furnace_load_status",
            "operation_code", "temperature", "actual_start_time", "actual_end_time",
            "serial_no", "job_no", "part_no", "part_name", "quantity")
    out = {k: row.get(k) for k in keep}
    out["confirmed"] = bool(out["confirmed"])
    cj = row.get("containers_json")
    out["containers"] = json.loads(cj) if cj else []
    out["stale"] = age is None or age > PLEX_STALE_AFTER_S
    out["age_s"] = age
    return out


def _value_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "other"


def _door_closed(snapshot, side):
    """True/False/None for whether a door is actually closed.

    side: "entrance" or "exit". The raw {side}_door_down field is confirmed
    wired normally-closed (see collector_config.CONFIRMED_ACTIVE_LOW) -
    invert it to get the real state. The raw field itself is untouched in
    `fields`; this is purely a derived value for display.
    """
    raw = snapshot.get(f"{side}_door_down")
    if raw is None or not isinstance(raw, (bool, int)):
        return None
    return not bool(raw)


def _best_remaining_min(oven, row, snapshot, oven_id, now):
    """The best available "time remaining" for this oven - see the identical
    function in store_sqlite.py for the full reasoning (large oven's native
    countdown is trusted directly; small oven uses the recipe-based
    calculation instead).
    """
    if oven.get("cycle_time_left_min_trusted"):
        return row.get("cycle_time_left_min") if row.get("state") == "RUNNING" else None
    return cycle_time.compute_remaining_min(
        snapshot, row.get("state"), _current_step_anchor(oven_id, snapshot.get("current_step")), now)


def _current_step_anchor(oven_id, current_step):
    """steady_reached_ts for the step_events document matching the CURRENT
    step - see the identical function in store_sqlite.py for why a
    mismatched step_number must return None rather than a stale anchor.
    """
    if current_step is None:
        return None
    row = get_db().step_events.find_one(
        {"oven_id": oven_id}, sort=[("ramp_start_ts", -1)], projection={"_id": False}
    )
    if row is None or row.get("step_number") != current_step or not row.get("steady_reached_ts"):
        return None
    return _parse_ts(row["steady_reached_ts"])


def _probe_valid(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value != collector_config.INVALID_TC_F
    )
