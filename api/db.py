"""MongoDB connection helper.

Matches the connection pattern already used by granco_monitor and the
other sibling projects: username and cluster host inline, only the
password read from an env var (SQL_PASS, a Render env var once deployed)
rather than a full connection-string env var. The database name is not in
the connection string either - it is picked explicitly in code, so this
project gets its own database (oven_monitor) rather than landing in
whatever the default happens to be.
"""
import os

from pymongo import MongoClient

DB_NAME = "oven_monitor"

_client = None


def get_db():
    global _client
    if _client is None:
        sql_pass = os.environ["SQL_PASS"]
        _client = MongoClient(
            f"mongodb+srv://padpress1:{sql_pass}@cluster0.ywwxl.mongodb.net/"
            "?retryWrites=true&w=majority&appName=Cluster0"
        )
    return _client[DB_NAME]


def ensure_indexes():
    db = get_db()
    # source_id is what makes /ingest idempotent - the publisher can re-send
    # the same rows after a failed POST without creating duplicates.
    db.samples.create_index("source_id", unique=True)
    db.samples.create_index([("oven_id", 1), ("ts", -1)])
    db.state_events.create_index("source_id", unique=True)
    db.state_events.create_index([("oven_id", 1), ("ts_start", 1)])
    db.plex_loads.create_index("source_id", unique=True)
    db.plex_loads.create_index([("oven_id", 1), ("ts", -1)])
    db.step_events.create_index("source_id", unique=True)
    db.step_events.create_index([("oven_id", 1), ("ramp_start_ts", -1)])
