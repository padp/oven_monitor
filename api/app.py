"""Read-only HTTP API for the oven monitor.

Mirrors granco_monitor's api/ in shape and will mirror its deployment
(Flask on Render, MongoDB Atlas) once the publisher exists. Until then it
serves the collector's local SQLite directly - see api/store.py - so the
dashboard is useful now rather than after the whole cloud pipeline lands.

Read-only on purpose: there is no /ingest here yet, because nothing
publishes yet. Adding one later does not change any endpoint below.
"""
import os

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from . import store
except ImportError:
    # Same trap as collector/collector.py: running this file directly makes
    # the relative import fail with a message that does not say why.
    if __package__ in (None, ""):
        raise SystemExit(
            "api/app.py is a package module and cannot be run directly.\n"
            "Run it from the project root instead:\n"
            "    python run_api.py\n"
            "or, equivalently:\n"
            "    python -m api.app"
        )
    raise

app = Flask(__name__)
CORS(app)

# Tables the publisher is allowed to write. An explicit allowlist rather
# than trusting the request body's keys, so a malformed or hostile payload
# cannot create arbitrary collections.
INGESTABLE = ("samples", "state_events", "plex_loads", "step_events")

if os.environ.get("SQL_PASS"):
    # Skipped when SQL_PASS is not set (local SQLite mode, or Render's build
    # step). At import time so it also runs under gunicorn, not just
    # `python run_api.py`.
    from .db import ensure_indexes
    ensure_indexes()


def _require_api_key():
    expected = os.environ.get("INGEST_API_KEY")
    if not expected:
        return False
    return request.headers.get("X-Api-Key") == expected


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "backend": store.backend_name()})


@app.post("/ingest")
def ingest():
    """Accept a batch of collector rows from the publisher.

    Upserts by source_id so re-delivery is harmless - the publisher only
    advances its checkpoint after a confirmed 200, which means a POST that
    succeeds server-side but fails in transit gets sent again.
    """
    if not _require_api_key():
        return jsonify(error="unauthorized"), 401

    from pymongo import UpdateOne
    from .db import get_db

    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    counts = {}

    for table_name in INGESTABLE:
        rows = body.get(table_name) or []
        usable = [r for r in rows if isinstance(r, dict) and r.get("source_id")]
        if usable:
            db[table_name].bulk_write(
                [UpdateOne({"source_id": r["source_id"]}, {"$set": r}, upsert=True)
                 for r in usable],
                ordered=False,
            )
        counts[table_name] = len(usable)
        if len(usable) != len(rows):
            # Silently dropping rows would look like data loss later; say so.
            counts[table_name + "_skipped_no_source_id"] = len(rows) - len(usable)

    return jsonify(ok=True, counts=counts)


@app.route("/api/ovens")
def list_ovens():
    return jsonify({"ovens": store.ovens()})


@app.route("/api/oven/<oven_id>/current")
def oven_current(oven_id):
    data = store.current(oven_id)
    if data is None:
        return jsonify({"error": "unknown oven: %s" % oven_id}), 404
    return jsonify(data)


@app.route("/api/oven/<oven_id>/history")
def oven_history(oven_id):
    """?hours=N for the live chart (last N hours), or ?start=<iso>&end=<iso>
    for reviewing a specific past load's absolute window - see store.history.
    """
    hours = request.args.get("hours", default=6, type=float)
    start = request.args.get("start")
    end = request.args.get("end")
    return jsonify({"samples": store.history(oven_id, hours=hours, start=start, end=end)})


@app.route("/api/oven/<oven_id>/states")
def oven_states(oven_id):
    hours = request.args.get("hours", default=24, type=float)
    return jsonify(store.states(oven_id, hours=hours))


@app.route("/api/oven/<oven_id>/job")
def oven_job(oven_id):
    """Current Plex job context, synced separately from PLC telemetry by
    plex_sync.py (see there for why - Plex latency and dashboard refresh
    rate are incompatible with calling it live on every request).

    A list, not a single object - almost always one item, but the dual-
    program workaround (see collector/plex.py's get_current_loads()) can
    leave two loads simultaneously Started for the same oven, and both
    need to be shown."""
    loads = store.current_plex_loads(oven_id)
    return jsonify({"loads": loads})


@app.route("/api/oven/<oven_id>/loads")
def oven_loads(oven_id):
    """Past Plex loads for the historical chart picker."""
    limit = request.args.get("limit", default=30, type=int)
    return jsonify({"loads": store.recent_loads(oven_id, limit=limit)})


def serve(host="0.0.0.0", port=8000):
    """Run the API.

    Binds all interfaces so the dashboard can be opened from any desk on
    the LAN - the collector runs on the poller host, and its SQLite file
    is on that machine's local disk, so the API has to live there too
    until the publisher and cloud API exist.

    Prefers waitress: this runs as a 24/7 service, and Flask's built-in
    server is a development server that says so on every start. Falls
    back to it anyway rather than refusing to start, since a dashboard
    that runs is worth more than one that is architecturally pure.
    """
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("waitress not installed - falling back to the Flask dev server")
        app.run(host=host, port=port, debug=False, threaded=True)
        return
    print("Serving oven API on http://%s:%d" % (host, port))
    waitress_serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    serve()
