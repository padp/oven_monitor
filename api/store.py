"""Read-side access to collected oven data.

Currently backed by the collector's local SQLite file. The cloud pipeline
(publisher -> Flask on Render -> MongoDB Atlas, mirroring granco_monitor)
is not stood up yet, so the API reads the same DB the collector writes.

Every function here returns plain dicts/lists in the shape the HTTP layer
serves, so swapping the backing store later means reimplementing this
module and nothing else - api/app.py never sees SQL or a driver.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import config  # noqa: E402

# How many poll intervals may pass before the newest sample is considered
# stale. Two gives one missed poll of slack before the dashboard starts
# claiming the collector is down.
STALE_AFTER_S = config.POLL_INTERVAL_S * 2.5


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


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
    age = (datetime.now() - ts).total_seconds() if ts else None

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
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
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
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts_start, ts_end, state, reason FROM state_events
               WHERE oven_id = ? AND (ts_end IS NULL OR ts_end >= ?)
               ORDER BY ts_start""",
            (oven_id, cutoff),
        ).fetchall()

    now = datetime.now()
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


def _value_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "other"


def _probe_valid(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value != config.INVALID_TC_F
    )
