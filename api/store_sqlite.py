"""Read-side access to collected oven data, backed by local SQLite.

Used when the API runs beside the collector (local development, or a
LAN-only deployment). The cloud deployment uses store_mongo instead -
api/store.py picks between them. Both expose the same four functions and
return the same shapes, so api/app.py never learns which is in play.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import config  # noqa: E402

# How many poll intervals may pass before the newest sample is considered
# stale. Two-and-a-half gives some slack for a missed poll before the
# dashboard starts claiming the collector is down.
#
# Plus a flat allowance on top for clock drift: the poller host's system
# clock has been observed running a couple of minutes behind true time
# (2026-08-27), and syncing it isn't straightforward - it's a domain-joined
# machine where time sync is managed by the org's policy, not a local
# Settings toggle. Age is computed as (this process's clock) - (the
# collector's own clock at write time), so that drift shows up directly as
# apparent staleness even when the collector is polling perfectly on
# schedule. 5 minutes covers the observed drift with real margin.
CLOCK_DRIFT_ALLOWANCE_S = 5 * 60
STALE_AFTER_S = config.POLL_INTERVAL_S * 2.5 + CLOCK_DRIFT_ALLOWANCE_S


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def ovens():
    """Every configured oven, whether or not it is being polled."""
    out = []
    with _connect() as conn:
        for oven in config.OVENS.values():
            row = conn.execute(
                "SELECT ts, state FROM samples WHERE oven_id = ? ORDER BY id DESC LIMIT 1",
                (oven["id"],),
            ).fetchone()
            out.append({
                "id": oven["id"],
                "name": oven["name"],
                "ip": oven["ip"],
                "enabled": oven["enabled"],
                "last_sample_ts": row["ts"] if row else None,
                "state": row["state"] if row else None,
            })
    return out


def current(oven_id):
    """Latest sample for one oven, with per-field provenance.

    Each field carries the PLC tag it came from and whether its polarity
    is still unconfirmed, so the dashboard can show a raw bit without
    implying the reading means what its name suggests.
    """
    oven = config.OVENS.get(oven_id)
    if oven is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM samples WHERE oven_id = ? ORDER BY id DESC LIMIT 1",
            (oven_id,),
        ).fetchone()
    if row is None:
        return {
            "oven": {"id": oven["id"], "name": oven["name"], "ip": oven["ip"],
                     "enabled": oven["enabled"]},
            "sample": None,
            "stale": True,
            "age_s": None,
            "fields": [],
            "load_temps": [],
        }

    snapshot = json.loads(row["snapshot_json"])
    load_temps = json.loads(row["load_temps_json"]) if row["load_temps_json"] else {}

    field_to_tag = {field: tag for tag, field in oven["tags"].items()}
    fields = []
    for field, value in snapshot.items():
        fields.append({
            "field": field,
            "tag": field_to_tag.get(field),
            "value": value,
            "type": _value_type(value),
            "polarity_unconfirmed": field in config.ACTIVE_LOW_SUSPECTS,
        })
    fields.sort(key=lambda f: f["field"])

    ts = _parse_ts(row["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None

    return {
        "oven": {"id": oven["id"], "name": oven["name"], "ip": oven["ip"],
                 "enabled": oven["enabled"]},
        "sample": {
            "ts": row["ts"],
            "state": row["state"],
            "reason": row["reason"],
            "zone1_temp": row["zone1_temp"],
            "zone2_temp": row["zone2_temp"],
            "setpoint": row["setpoint"],
            "entrance_door_closed": _door_closed(snapshot, "entrance"),
            "exit_door_closed": _door_closed(snapshot, "exit"),
            "zone1_burner": row["zone1_burner"],
            "zone2_burner": row["zone2_burner"],
            "cycle_time_left_min": row["cycle_time_left_min"],
            "exhaust_fan_active": row["exhaust_fan_active"],
            "load_temp_min": row["load_temp_min"],
            "load_temp_mean": row["load_temp_mean"],
            "load_temp_max": row["load_temp_max"],
            "load_temp_valid_count": row["load_temp_valid_count"],
        },
        "stale": age is None or age > STALE_AFTER_S,
        "age_s": age,
        "poll_interval_s": config.POLL_INTERVAL_S,
        "fields": fields,
        "load_temps": [
            {"probe": name, "value": value,
             "valid": _probe_valid(value)}
            for name, value in load_temps.items()
        ],
    }


def history(oven_id, hours=6, limit=2000):
    """Time series for charting. Newest last."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts, state, zone1_temp, zone2_temp, setpoint, zone1_burner,
                      zone2_burner, cycle_time_left_min, exhaust_fan_active,
                      load_temp_mean, load_temp_min, load_temp_max
               FROM samples WHERE oven_id = ? AND ts >= ?
               ORDER BY id DESC LIMIT ?""",
            (oven_id, cutoff, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def states(oven_id, hours=24):
    """State segments plus a duration rollup, for uptime reporting."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts_start, ts_end, state, reason FROM state_events
               WHERE oven_id = ? AND (ts_end IS NULL OR ts_end >= ?)
               ORDER BY ts_start""",
            (oven_id, cutoff),
        ).fetchall()

    now = datetime.now(timezone.utc)
    segments = []
    totals = {}
    for r in rows:
        start = _parse_ts(r["ts_start"])
        end = _parse_ts(r["ts_end"]) if r["ts_end"] else now
        if start is None or end is None:
            continue
        duration = max((end - start).total_seconds(), 0.0)
        segments.append({
            "ts_start": r["ts_start"],
            "ts_end": r["ts_end"],
            "state": r["state"],
            "reason": r["reason"],
            "duration_s": duration,
            "open": r["ts_end"] is None,
        })
        totals[r["state"]] = totals.get(r["state"], 0.0) + duration

    observed = sum(totals.values())
    return {
        "segments": segments,
        "totals_s": totals,
        "observed_s": observed,
        # Deliberately not called "uptime": with one oven newly online and
        # no confirmed cycle yet, a percentage would imply more than the
        # data supports. It is the share of OBSERVED time, nothing more.
        "share_pct": {k: (100.0 * v / observed if observed else 0.0)
                      for k, v in totals.items()},
    }


# plex_sync.py runs every 120s and a cycle itself can take ~20s+ (Plex
# latency, occasionally a fresh login) - much slower than the PLC's 30s
# loop, so this needs its own, more generous staleness allowance rather
# than reusing STALE_AFTER_S.
PLEX_STALE_AFTER_S = 10 * 60


def current_plex_load(oven_id):
    """Latest Plex job-context lookup for one oven, or None if plex_sync.py
    has never run (or never found a plex_workcenter_key configured) for it.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM plex_loads WHERE oven_id = ? ORDER BY id DESC LIMIT 1",
            (oven_id,),
        ).fetchone()
    if row is None:
        return None

    ts = _parse_ts(row["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None

    return {
        "ts": row["ts"],
        "confirmed": bool(row["confirmed"]),
        "furnace_load_no": row["furnace_load_no"],
        "furnace_load_status": row["furnace_load_status"],
        "operation_code": row["operation_code"],
        "temperature": row["temperature"],
        "actual_start_time": row["actual_start_time"],
        "actual_end_time": row["actual_end_time"],
        "serial_no": row["serial_no"],
        "job_no": row["job_no"],
        "part_no": row["part_no"],
        "part_name": row["part_name"],
        "quantity": row["quantity"],
        "stale": age is None or age > PLEX_STALE_AFTER_S,
        "age_s": age,
    }


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
    wired normally-closed (see config.CONFIRMED_ACTIVE_LOW) - invert it to
    get the real state. The raw field itself is untouched in `fields`; this
    is purely a derived value for display.
    """
    raw = snapshot.get(f"{side}_door_down")
    if raw is None or not isinstance(raw, (bool, int)):
        return None
    return not bool(raw)


def _probe_valid(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value != config.INVALID_TC_F
    )
