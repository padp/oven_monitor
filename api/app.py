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

from . import store

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
