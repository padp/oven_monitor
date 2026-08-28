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
from . import cycle_time  # noqa: E402

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
    now = datetime.now(timezone.utc)

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
            "cycle_time_remaining_computed_min": _best_remaining_min(oven, row, snapshot, oven_id, now),
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


def history(oven_id, hours=6, limit=5000, start=None, end=None):
    """Time series for charting. Newest last.

    Two modes: the live chart wants "the last `hours`", relative to now
    (start/end omitted). Reviewing a specific past load wants an explicit
    absolute window instead - that load may be days old, so "hours back
    from now" cannot express it. Passing start/end (ISO strings) overrides
    the hours-based cutoff entirely rather than adding a second, easily
    inconsistent code path.
    """
    if start is None:
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    query = """SELECT ts, state, zone1_temp, zone2_temp, setpoint, zone1_burner,
                      zone2_burner, cycle_time_left_min, exhaust_fan_active,
                      load_temp_mean, load_temp_min, load_temp_max
               FROM samples WHERE oven_id = ? AND ts >= ?"""
    params = [oven_id, start]
    if end is not None:
        query += " AND ts <= ?"
        params.append(end)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def recent_loads(oven_id, limit=30):
    """Past Plex loads for this oven, most recent first - the historical
    chart picker's list. One row per plex_sync.py sync tick that saw a
    given furnace_load_no, so this naturally has duplicates per load
    (re-synced every ~2 minutes while it's the current one); collapsed
    here to one entry per distinct furnace_load_no, keeping its MOST
    RECENT row - a load's status can genuinely change between syncs
    (Started -> Completed), and the picker should show that, not a
    stale first-seen status.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT furnace_load_no, furnace_load_status, confirmed, program_number,
                      part_no, part_name, actual_start_time, actual_end_time, ts
               FROM plex_loads WHERE oven_id = ? AND furnace_load_no IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (oven_id, limit * 6),  # oversample before dedup - see docstring
        ).fetchall()
    seen = {}
    for r in rows:
        # DESC order: the first time a furnace_load_no is encountered here
        # IS its most recent row, so later (older) duplicates must not
        # overwrite it.
        if r["furnace_load_no"] not in seen:
            seen[r["furnace_load_no"]] = dict(r)
    ordered = sorted(seen.values(), key=lambda r: r["ts"], reverse=True)
    return ordered[:limit]


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


def _job_from_row(row):
    ts = _parse_ts(row["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None
    return {
        "ts": row["ts"],
        "confirmed": bool(row["confirmed"]),
        "furnace_load_no": row["furnace_load_no"],
        "furnace_load_status": row["furnace_load_status"],
        "operation_code": row["operation_code"],
        # row["program_number"] would raise on a database whose collector
        # has not restarted since this column was added (see _migrate()) -
        # membership-check rather than crash the whole endpoint meanwhile.
        "program_number": row["program_number"] if "program_number" in row.keys() else None,
        "temperature": row["temperature"],
        "actual_start_time": row["actual_start_time"],
        "actual_end_time": row["actual_end_time"],
        "serial_no": row["serial_no"],
        "job_no": row["job_no"],
        "part_no": row["part_no"],
        "part_name": row["part_name"],
        "quantity": row["quantity"],
        "containers": json.loads(row["containers_json"]) if row["containers_json"] else [],
        "stale": age is None or age > PLEX_STALE_AFTER_S,
        "age_s": age,
    }


def current_plex_loads(oven_id):
    """Every Plex load presently current for one oven - almost always one,
    occasionally two (the dual-program workaround - see collector/plex.py's
    get_current_loads() - can leave two loads simultaneously Started).

    plex_sync.py gives every load found in the same sync tick the exact
    same ts (see its sync_once()), so "everything from the latest tick" is
    an exact ts match against the newest row, not a time-window guess.
    Returns [] if plex_sync.py has never run (or never found a
    plex_workcenter_key configured) for this oven.
    """
    with _connect() as conn:
        latest = conn.execute(
            "SELECT ts FROM plex_loads WHERE oven_id = ? ORDER BY id DESC LIMIT 1",
            (oven_id,),
        ).fetchone()
        if latest is None:
            return []
        rows = conn.execute(
            "SELECT * FROM plex_loads WHERE oven_id = ? AND ts = ? ORDER BY furnace_load_no",
            (oven_id, latest["ts"]),
        ).fetchall()
    return [_job_from_row(row) for row in rows]


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


def _best_remaining_min(oven, row, snapshot, oven_id, now):
    """The best available "time remaining" for this oven.

    Two mechanisms, picked per-oven by config.OVENS[...]["cycle_time_left_min_trusted"]:
    the large oven's CYCLE_TOTAL_MINUTES_LEFT genuinely counts down (confirmed
    live 2026-08-27: 350->349->349->349 over 60s during a real cycle) - use it
    directly rather than a recipe-based calculation it does not need. The small
    oven's equivalent tag is a frozen setpoint, so it uses the recipe-based
    compute_remaining_min() instead. Only trusted while RUNNING either way, for
    the same reason compute_remaining_min gates on it: an idle oven's last-known
    values do not mean a cycle is actually in progress.
    """
    if oven.get("cycle_time_left_min_trusted"):
        return row["cycle_time_left_min"] if row["state"] == "RUNNING" else None
    return cycle_time.compute_remaining_min(
        snapshot, row["state"], _current_step_anchor(oven_id, snapshot.get("current_step")), now)


def _current_step_anchor(oven_id, current_step):
    """steady_reached_ts for the step_events row matching the CURRENT step.

    Only trusts the latest row if its step_number matches - if the
    collector has not yet recorded the current step (a brief lag right
    after a step transition) this returns None rather than a stale
    timestamp from a previous, different step, which cycle_time's caller
    treats as "no anchor yet" (full soak remaining, not a wrong elapsed
    time from the wrong step).
    """
    if current_step is None:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT step_number, steady_reached_ts FROM step_events
                   WHERE oven_id = ? ORDER BY id DESC LIMIT 1""",
                (oven_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        # step_events does not exist yet on this database - an older
        # collector build that has not restarted with the current schema.
        # No anchor is exactly the right answer here, same as "no rows yet".
        return None
    if row is None or row["step_number"] != current_step or row["steady_reached_ts"] is None:
        return None
    return _parse_ts(row["steady_reached_ts"])


def _probe_valid(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value != config.INVALID_TC_F
    )
