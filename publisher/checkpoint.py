"""Tracks the last-synced row id per table.

Kept in its own small SQLite file, separate from the collector's
oven_monitor.db, so the publisher never writes to the DB the collector is
actively writing to - it only ever reads from it.
"""
import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_checkpoint (
    table_name TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
);
"""


class Checkpoint:
    def __init__(self, db_path: str = config.CHECKPOINT_DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def last_id(self, table_name: str) -> int:
        row = self._conn.execute(
            "SELECT last_id FROM sync_checkpoint WHERE table_name = ?", (table_name,)
        ).fetchone()
        return row[0] if row else 0

    def advance(self, table_name: str, last_id: int):
        self._conn.execute(
            """INSERT INTO sync_checkpoint (table_name, last_id) VALUES (?, ?)
               ON CONFLICT(table_name) DO UPDATE SET last_id = excluded.last_id""",
            (table_name, last_id),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
