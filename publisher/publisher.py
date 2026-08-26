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


def _source_id(table_name, row):
    """A stable identity for a row, independent of local rowids.

    Deliberately NOT the SQLite id: if the local DB is ever rebuilt (a
    reinstall, a moved host, a corrupted file) ids restart from 1 and would
    silently overwrite unrelated cloud documents. oven_id plus the row's own
    timestamp is unique by construction - one sample per oven per poll, one
    state segment per oven per start instant - and survives a rebuild.
    """
    stamp = row["ts"] if table_name == "samples" else row["ts_start"]
    return "%s:%s" % (row["oven_id"], stamp)


def _fetch(conn, table_name, last_id, limit):
    if table_name == "state_events":
        # state_events rows are MUTATED after insert: ts_end is filled in when
        # the segment closes. A plain "id > last_id" sweep would publish the
        # open segment once, with ts_end null, and never correct it - so the
        # cloud would show every state as still running. Re-send anything still
        # open, every tick, until it closes.
        cur = conn.execute(
            "SELECT * FROM state_events WHERE id > ? OR ts_end IS NULL ORDER BY id LIMIT ?",
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

    For append-only tables, the highest id sent. For state_events, no further
    than just below the oldest row that is still open - everything below that
    is closed and final, while the open ones must keep being re-sent. Never
    moves backwards.
    """
    sent_max = max(r["id"] for r in rows)
    if table_name != "state_events":
        return max(sent_max, last_id)
    open_ids = [r["id"] for r in rows if r.get("ts_end") is None]
    candidate = (min(open_ids) - 1) if open_ids else sent_max
    return max(candidate, last_id)


def sync_once(conn, checkpoint, api_url, api_key):
    payload = {}
    next_ids = {}
    for table_name in ("samples", "state_events"):
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
