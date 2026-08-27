"""SQLite schema and storage helpers for the oven monitor.

Two tables, both keyed by oven_id so a second oven is a config change
rather than a schema change:

  samples       one row per poll per oven - the continuous telemetry
                record, and the direct successor to the legacy daily
                Large_Oven_Status_*.json files
  state_events  RUNNING/IDLE/FAULT/UNKNOWN segments, for uptime reporting

Unlike the saw monitor - where raw polls are a 48h troubleshooting buffer
and the real record is discrete cut events - an oven has no equivalent
discrete event. The telemetry IS the record: uptime, cycle behaviour and
the still-unsettled bit-polarity question are all answered by looking
back over continuous samples. So `samples` is kept, not pruned, by
default. At 30s per oven that is ~2900 rows/day, which SQLite does not
care about.

Every value in `samples` is stored EXACTLY as the PLC reported it. No
polarity correction, no invalid-value substitution beyond dropping known
dead thermocouples out of the load aggregates. Interpretation happens at
read time, so a wrong assumption today cannot corrupt the history that
would prove it wrong.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oven_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    state TEXT,
    reason TEXT,
    zone1_temp REAL,
    zone2_temp REAL,
    setpoint REAL,
    zone1_burner REAL,
    zone2_burner REAL,
    cycle_time_left_min REAL,
    exhaust_fan_active REAL,
    load_temp_min REAL,
    load_temp_mean REAL,
    load_temp_max REAL,
    load_temp_valid_count INTEGER,
    snapshot_json TEXT NOT NULL,
    load_temps_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_oven_ts ON samples(oven_id, ts);

CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oven_id TEXT NOT NULL,
    ts_start TEXT NOT NULL,
    ts_end TEXT,
    state TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_events_oven ON state_events(oven_id, ts_start);

CREATE TABLE IF NOT EXISTS plex_loads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oven_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    furnace_load_no TEXT,
    furnace_load_status TEXT,
    operation_code TEXT,
    temperature REAL,
    actual_start_time TEXT,
    actual_end_time TEXT,
    serial_no TEXT,
    job_no TEXT,
    part_no TEXT,
    part_name TEXT,
    quantity REAL,
    containers_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_plex_loads_oven_ts ON plex_loads(oven_id, ts);

CREATE TABLE IF NOT EXISTS step_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oven_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    ramp_start_ts TEXT NOT NULL,
    target_temp REAL,
    ramp_rate REAL,
    soak_duration_min REAL,
    steady_reached_ts TEXT,
    steady_reached_temp REAL
);
CREATE INDEX IF NOT EXISTS idx_step_events_oven ON step_events(oven_id, ramp_start_ts);
"""

# Canonical fields promoted to real columns because they are what almost
# every query filters or plots on. Everything else still lands in
# snapshot_json, so nothing is lost by not being listed here.
SAMPLE_COLUMNS = [
    "zone1_temp",
    "zone2_temp",
    "setpoint",
    "zone1_burner",
    "zone2_burner",
    "cycle_time_left_min",
    "exhaust_fan_active",
]


class Storage:
    def __init__(self, db_path: str = config.DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        # Open state segment per oven, so two ovens don't share one.
        self._open_state = {}
        # Open (still-ramping) recipe step per oven - see record_step().
        self._open_step = {}

    def _migrate(self):
        """Add columns introduced after a db file already existed.

        CREATE TABLE IF NOT EXISTS only creates a table that is missing
        entirely - it does nothing to a table that already exists but is
        missing a column a newer schema added, which is exactly what
        happened when containers_json was added to plex_loads after this
        project's real production database already had rows in it.
        """
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(plex_loads)")}
        if existing and "containers_json" not in existing:
            self._conn.execute("ALTER TABLE plex_loads ADD COLUMN containers_json TEXT")

    # --- samples ------------------------------------------------------

    def insert_sample(self, oven_id, ts, snapshot, state, reason, load_temps):
        """Record one poll. `snapshot` is the full canonical dict."""
        stats = summarize_load_temps(load_temps)
        cols = ["oven_id", "ts", "state", "reason"] + SAMPLE_COLUMNS + [
            "load_temp_min", "load_temp_mean", "load_temp_max",
            "load_temp_valid_count", "snapshot_json", "load_temps_json",
        ]
        values = [oven_id, ts.isoformat(), state, reason]
        values += [_as_number(snapshot.get(c)) for c in SAMPLE_COLUMNS]
        values += [
            stats["min"], stats["mean"], stats["max"], stats["valid_count"],
            json.dumps(snapshot, default=str),
            json.dumps(load_temps, default=str) if load_temps else None,
        ]
        placeholders = ",".join("?" * len(cols))
        self._conn.execute(
            "INSERT INTO samples (%s) VALUES (%s)" % (",".join(cols), placeholders),
            values,
        )
        self._conn.commit()

    # --- Plex job context -----------------------------------------------

    def insert_plex_load(self, oven_id, ts, load):
        """Record one Plex lookup result.

        `load` is the flattened dict collector/plex_sync.py builds from
        get_current_load() + get_container() - see there for the shape.
        Appended, like samples, rather than upserted in place: a history of
        what was running (or guessed to be running) over time is exactly
        the kind of thing worth keeping, and it costs nothing at this
        polling rate (every couple of minutes, not every 30s).
        """
        cols = [
            "oven_id", "ts", "confirmed", "furnace_load_no", "furnace_load_status",
            "operation_code", "temperature", "actual_start_time", "actual_end_time",
            "serial_no", "job_no", "part_no", "part_name", "quantity", "containers_json",
        ]
        values = [oven_id, ts.isoformat(), int(load.get("confirmed", False))]
        values += [load.get(c) for c in cols[3:-1]]
        values += [json.dumps(load.get("containers", []), default=str)]
        placeholders = ",".join("?" * len(cols))
        self._conn.execute(
            "INSERT INTO plex_loads (%s) VALUES (%s)" % (",".join(cols), placeholders),
            values,
        )
        self._conn.commit()

    # --- state segments -----------------------------------------------

    def record_state(self, oven_id, ts, state, reason):
        """Open a new state segment when the state changes, closing the last.

        No-op while the state is unchanged, so a 6-hour RUNNING stretch is
        one row rather than 720.
        """
        current = self._open_state.get(oven_id)
        if current is not None and current["state"] == state:
            return
        if current is not None:
            self._conn.execute(
                "UPDATE state_events SET ts_end = ? WHERE id = ?",
                (ts.isoformat(), current["id"]),
            )
        cur = self._conn.execute(
            "INSERT INTO state_events (oven_id, ts_start, state, reason) VALUES (?,?,?,?)",
            (oven_id, ts.isoformat(), state, reason),
        )
        self._open_state[oven_id] = {"id": cur.lastrowid, "state": state}
        self._conn.commit()

    def resume_open_states(self):
        """Re-attach to any state segment left open by a previous run.

        Without this, restarting the collector closes nothing and opens a
        duplicate segment for a state the oven never actually re-entered,
        which would show up as a spurious transition in uptime reporting.
        """
        rows = self._conn.execute(
            "SELECT id, oven_id, state FROM state_events WHERE ts_end IS NULL"
        ).fetchall()
        for row_id, oven_id, state in rows:
            self._open_state[oven_id] = {"id": row_id, "state": state}

    # --- recipe steps ---------------------------------------------------
    #
    # A step_events row anchors the two moments the remaining-time
    # calculation needs (see api/store_*.py's compute_cycle_remaining):
    # ramp_start_ts (when this step began) and steady_reached_ts (when
    # ramp finished and soak began - NULL until then). The row is written
    # once and updated at most once more, then permanently done - the
    # exact lifecycle state_events already has (ts_end NULL -> filled in
    # once). The calculation reads steady_reached_ts fresh from the
    # database on every request rather than from collector memory, so a
    # collector restart mid-soak loses nothing: the anchor already
    # written to disk is what "picking back up where we left off" means
    # here - there is no separate recovery step needed for it.
    #
    # resume_open_steps() below exists only to stop a restart from
    # inserting a DUPLICATE row for a step that was already mid-ramp -
    # it has no bearing on soak recovery, which the database already
    # handles by simply being the source of truth record_step() never
    # needs to duplicate in memory.

    def resume_open_steps(self):
        """Re-attach to the most recent step_events row per oven, whether
        it is still ramping or already closed (steady reached).

        Resuming only the still-open rows is not enough: if the last row
        was already CLOSED before a restart (soak had already begun) and
        the next poll reports the same step_number with new_cycle=False,
        record_step needs to recognize "this is that same, already-closed
        step - nothing to do" rather than seeing no in-memory record at
        all and wrongly inserting a duplicate row for a step that has not
        actually restarted. Caught by testing a simulated restart
        mid-soak, not assumed.
        """
        rows = self._conn.execute(
            """SELECT id, oven_id, step_number, steady_reached_ts FROM step_events
               WHERE id IN (SELECT MAX(id) FROM step_events GROUP BY oven_id)"""
        ).fetchall()
        for row_id, oven_id, step_number, steady_reached_ts in rows:
            self._open_step[oven_id] = {
                "id": row_id, "step_number": step_number,
                "closed": steady_reached_ts is not None,
            }

    def record_step(self, oven_id, ts, step_number, target_temp, ramp_rate,
                     soak_duration_min, at_steady, actual_temp, new_cycle):
        """Track one recipe step's ramp-start and steady-reached timestamps.

        new_cycle: True exactly when the caller detects a fresh
        non-RUNNING -> RUNNING transition. Needed because step_number
        alone is not a reliable "is this the same step instance" key - a
        later, unrelated load can legitimately reuse the same step number
        (most recipes seen so far are single-step), so relying on
        step_number matching alone would wrongly keep appending to a step
        from a load that already finished. new_cycle forces a fresh row
        regardless of what step_number the new load happens to start on.
        """
        current = self._open_step.get(oven_id)
        is_new = new_cycle or current is None or current["step_number"] != step_number
        if is_new:
            cur = self._conn.execute(
                """INSERT INTO step_events
                   (oven_id, step_number, ramp_start_ts, target_temp, ramp_rate, soak_duration_min)
                   VALUES (?,?,?,?,?,?)""",
                (oven_id, step_number, ts.isoformat(), target_temp, ramp_rate, soak_duration_min),
            )
            self._open_step[oven_id] = {"id": cur.lastrowid, "step_number": step_number, "closed": False}
            self._conn.commit()
            current = self._open_step[oven_id]

        if not current.get("closed") and at_steady:
            self._conn.execute(
                "UPDATE step_events SET steady_reached_ts = ?, steady_reached_temp = ? WHERE id = ?",
                (ts.isoformat(), actual_temp, current["id"]),
            )
            current["closed"] = True
            self._conn.commit()

    # --- retention ----------------------------------------------------

    def prune_samples(self, now):
        """Drop samples older than the configured retention, if any.

        Defaults to keeping everything - see the module docstring. Set
        config.SAMPLE_RETENTION_DAYS once the cloud API is the long-term
        store and local SQLite is genuinely just a publish buffer.
        """
        days = getattr(config, "SAMPLE_RETENTION_DAYS", None)
        if not days:
            return
        cutoff = (now - timedelta(days=days)).isoformat()
        self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self._conn.commit()

    def close(self):
        self._conn.close()


def _as_number(value):
    """Coerce a PLC value to something SQLite will store in a REAL column.

    BOOLs arrive as Python bools and store as 0/1; byte blobs from an
    unexpectedly-structured tag are not numbers and are dropped to NULL
    here rather than raising - the full value still lands in
    snapshot_json either way.
    """
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, (int, float)):
        return value
    return None


def summarize_load_temps(load_temps):
    """Aggregate the load thermocouples, excluding known-dead probes.

    A probe pinned at config.INVALID_TC_F is at type J full scale, which
    means "no signal", not a real temperature - averaging it in would drag
    the load temperature up by hundreds of degrees. The card's own
    open-circuit and over-range bits do NOT flag this case, so the filter
    has to happen here.

    valid_count is stored alongside the aggregates so a load temperature
    derived from three surviving probes is distinguishable from one
    derived from all eleven.
    """
    empty = {"min": None, "mean": None, "max": None, "valid_count": 0}
    if not load_temps:
        return empty
    good = [
        v for v in load_temps.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and v != config.INVALID_TC_F
    ]
    if not good:
        return empty
    return {
        "min": min(good),
        "mean": sum(good) / len(good),
        "max": max(good),
        "valid_count": len(good),
    }
