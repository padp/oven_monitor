"""Main loop: forwards new local samples/state_events rows to the cloud API.

Only ever issues SELECTs against the collector's oven_monitor.db - the
collector is the only writer to that file. Network or API failures just
retry next tick; the checkpoint only advances on a confirmed successful
POST, so nothing is lost, and re-delivery of the same rows is harmless
(the API upserts by source_id).
"""
import sqlite3
import time

import requests

from . import config
from .checkpoint import Checkpoint


def _dict_rows(cur):
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# Tables whose rows are MUTATED after insert: `open_column` is NULL while
# the row is still open and gets filled in exactly once when it closes
# (state_events.ts_end when a segment ends, step_events.steady_reached_ts
# when ramp finishes and soak begins). A plain "id > last_id" sweep would
# publish an open row once, with the open column null, and never correct
# it once it closes - so both need the same "re-send anything still open,
# every tick, until it closes" handling. `start_column` is the row's
# natural per-instance key (the start of whatever span it represents) used
# as the stable part of source_id, since raw SQLite ids are not stable
# across a rebuilt local database.
MUTABLE_TABLES = {
    "state_events": {"open_column": "ts_end", "start_column": "ts_start"},
    "step_events": {"open_column": "steady_reached_ts", "start_column": "ramp_start_ts"},
}


def _source_id(table_name, row):
    """A stable identity for a row, independent of local rowids."""
    if table_name == "plex_loads":
        # plex_sync.py can insert more than one row per oven with the SAME
        # ts (the dual-program workaround - see get_current_loads() -
        # occasionally has two loads Started at once, sync'd in the same
        # tick on purpose so the API can group them back together). ts
        # alone would collide and the ingest upsert would silently drop
        # one; furnace_load_no disambiguates them.
        return "%s:%s:%s" % (row["oven_id"], row["ts"], row["furnace_load_no"])
    spec = MUTABLE_TABLES.get(table_name)
    stamp = row[spec["start_column"]] if spec else row["ts"]
    return "%s:%s" % (row["oven_id"], stamp)


def _fetch(conn, table_name, last_id, limit):
    spec = MUTABLE_TABLES.get(table_name)
    if spec:
        cur = conn.execute(
            "SELECT * FROM %s WHERE id > ? OR %s IS NULL ORDER BY id LIMIT ?"
            % (table_name, spec["open_column"]),
            (last_id, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM %s WHERE id > ? ORDER BY id LIMIT ?" % table_name,
            (last_id, limit),
        )
    return _dict_rows(cur)


def _next_checkpoint(table_name, rows, last_id):
    """How far the checkpoint may safely advance.

    For append-only tables, the highest id sent. For a mutable table, no
    further than just below the oldest row that is still open - everything
    below that is closed and final, while the open ones must keep being
    re-sent. Never moves backwards.
    """
    spec = MUTABLE_TABLES.get(table_name)
    sent_max = max(r["id"] for r in rows)
    if not spec:
        return max(sent_max, last_id)
    open_ids = [r["id"] for r in rows if r.get(spec["open_column"]) is None]
    candidate = (min(open_ids) - 1) if open_ids else sent_max
    return max(candidate, last_id)


def sync_once(conn, checkpoint, api_url, api_key):
    payload = {}
    next_ids = {}
    for table_name in ("samples", "state_events", "plex_loads", "step_events"):
        last_id = checkpoint.last_id(table_name)
        rows = _fetch(conn, table_name, last_id, config.BATCH_LIMIT)
        if not rows:
            continue
        for row in rows:
            row["source_id"] = _source_id(table_name, row)
        payload[table_name] = rows
        next_ids[table_name] = _next_checkpoint(table_name, rows, last_id)

    if not payload:
        return False

    response = requests.post(
        "%s/ingest" % api_url,
        json=payload,
        headers={"X-Api-Key": api_key},
        timeout=config.REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()

    for table_name, last_id in next_ids.items():
        checkpoint.advance(table_name, last_id)

    print("synced " + ", ".join("%d %s" % (len(rows), name)
                                for name, rows in payload.items()))
    return True


def run():
    api_url, api_key = config.load_api_config()
    conn = sqlite3.connect(config.COLLECTOR_DB_PATH)
    checkpoint = Checkpoint()

    print("Publishing from %s to %s every %gs" % (
        config.COLLECTOR_DB_PATH, api_url, config.SYNC_INTERVAL_S))

    try:
        while True:
            try:
                sync_once(conn, checkpoint, api_url, api_key)
            except (requests.RequestException, sqlite3.Error) as exc:
                print("sync error (will retry): %s" % exc)
            time.sleep(config.SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping publisher.")
    finally:
        conn.close()
        checkpoint.close()
