"""Read-only HTTP API for the oven monitor.

Mirrors granco_monitor's api/ in shape and will mirror its deployment
(Flask on Render, MongoDB Atlas) once the publisher exists. Until then it
serves the collector's local SQLite directly - see api/store.py - so the
dashboard is useful now rather than after the whole cloud pipeline lands.

Read-only on purpose: there is no /ingest here yet, because nothing
publishes yet. Adding one later does not change any endpoint below.
"""
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


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


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
    hours = request.args.get("hours", default=6, type=float)
    return jsonify({"samples": store.history(oven_id, hours=hours)})


@app.route("/api/oven/<oven_id>/states")
def oven_states(oven_id):
    hours = request.args.get("hours", default=24, type=float)
    return jsonify(store.states(oven_id, hours=hours))


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
