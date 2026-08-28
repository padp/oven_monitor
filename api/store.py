"""Picks the read-side backend and forwards to it.

Two implementations, same four functions, same return shapes:

  store_mongo   MongoDB Atlas - the deployed API on Render
  store_sqlite  the collector's local SQLite - local dev, or running the
                API beside the collector on the poller host

The choice is made by whether SQL_PASS is set, which is the same signal
db.py uses to decide it can connect at all. Render sets it; a laptop does
not. That keeps `python run_api.py` working with zero configuration while
the deployed instance needs no code change.

Resolved per call rather than cached at import, so a process that gains
or loses the env var (a test, a shell that exports it late) does not have
to be restarted to notice.
"""
import os


def _impl():
    if os.environ.get("SQL_PASS"):
        from . import store_mongo
        return store_mongo
    from . import store_sqlite
    return store_sqlite


def backend_name():
    return "mongo" if os.environ.get("SQL_PASS") else "sqlite"


def ovens():
    return _impl().ovens()


def current(oven_id):
    return _impl().current(oven_id)


def history(oven_id, hours=6, limit=5000, start=None, end=None):
    return _impl().history(oven_id, hours=hours, limit=limit, start=start, end=end)


def states(oven_id, hours=24):
    return _impl().states(oven_id, hours=hours)


def current_plex_load(oven_id):
    return _impl().current_plex_load(oven_id)


def recent_loads(oven_id, limit=30):
    return _impl().recent_loads(oven_id, limit=limit)
