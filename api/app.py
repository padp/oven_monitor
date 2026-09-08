"""Read-only HTTP API for the oven monitor.

Mirrors granco_monitor's api/ in shape and will mirror its deployment
(Flask on Render, MongoDB Atlas) once the publisher exists. Until then it
serves the collector's local SQLite directly - see api/store.py - so the
dashboard is useful now rather than after the whole cloud pipeline lands.

Read-only on purpose: there is no /ingest here yet, because nothing
publishes yet. Adding one later does not change any endpoint below.
"""
import os

from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, jsonify, request
from flask_cors import CORS

from collector import config as collector_config

try:
    from . import alerts, store
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
    if not os.environ.get("POLL_DISABLED"):
        # This repo's first in-process background thread - everything else
        # long-running here (collector, publisher, plex_sync) is a separate
        # OS process (NSSM/Scheduled Tasks), which can't evaluate alert
        # rules against a document living in this process's own Mongo
        # connection. Same POLL_DISABLED escape hatch picos/granco_monitor
        # use, for a local dev instance that shouldn't fire real Teams
        # messages while someone's just poking at the API by hand.
        alerts.start_background_alert_poller()


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


def _serialize_alert_rule(rule):
    rule["_id"] = str(rule["_id"])
    rule["webhook_url_masked"] = alerts.mask_webhook_url(rule.pop("webhook_url", None))
    for trigger in rule.get("triggers", []):
        trigger["description"] = alerts.describe_trigger(trigger)
    return rule


@app.get("/api/oven/<oven_id>/alerts/tags")
def oven_alerts_tags(oven_id):
    """Everything the trigger builder needs for one oven: every tag it can
    be built against (bool/numeric/string - see alerts.list_available_tags)
    plus the comparator/bool-mode wording and repeat-mode options."""
    err = alerts.validate_oven_id(oven_id)
    if err:
        return jsonify(error=err), 404
    return jsonify(
        tags=alerts.list_available_tags(oven_id),
        comparators=alerts.comparator_options(),
        bool_modes=alerts.bool_mode_options(),
        repeat_modes=list(alerts.REPEAT_MODES),
        has_default_webhook=bool(alerts.default_webhook_url()),
        recipient_email_domain=alerts.ALLOWED_RECIPIENT_DOMAIN,
    )


@app.get("/api/oven/<oven_id>/alerts")
def oven_alerts_list(oven_id):
    err = alerts.validate_oven_id(oven_id)
    if err:
        return jsonify(error=err), 404
    from .db import get_db
    db = get_db()
    rules = list(db.alert_rules.find({"oven_id": oven_id}, sort=[("created_at", -1)]))
    return jsonify(alerts=[_serialize_alert_rule(r) for r in rules])


@app.post("/api/oven/<oven_id>/alerts/describe")
def oven_alerts_describe(oven_id):
    """Read-only preview: validates a draft trigger and returns its
    human-readable description, without creating anything - see
    granco_monitor's identical route (this one doesn't need oven_id at
    all beyond the URL, since describe_draft_trigger has no oven-specific
    behavior - kept oven-scoped in the URL anyway for consistency with
    every other route here)."""
    payload = request.get_json(force=True, silent=True) or {}
    description, err = alerts.describe_draft_trigger(payload.get("trigger"))
    if err:
        return jsonify(error=err), 400
    return jsonify(description=description)


@app.post("/api/oven/<oven_id>/alerts/test-webhook")
def oven_alerts_test_webhook(oven_id):
    payload = request.get_json(force=True, silent=True) or {}
    webhook_url = payload.get("webhook_url")
    err = alerts.validate_webhook_url(webhook_url)
    if err:
        return jsonify(error=err), 400
    webhook_url = webhook_url or alerts.default_webhook_url()
    recipient_email = payload.get("recipient_email")
    err = alerts.validate_recipient_email(recipient_email)
    if err:
        return jsonify(error=err), 400
    oven_name = collector_config.OVENS.get(oven_id, {}).get("name", oven_id)
    ok, status, body = alerts.send_teams(
        webhook_url,
        f"{oven_name} - Test Alert",
        "This is a test message from the Oven Monitor Alerts page.",
        recipient_email=recipient_email,
    )
    if not ok:
        return jsonify(error=f"Teams rejected the request (status {status}): {body[:300]}"), 400
    return jsonify(ok=True)


@app.post("/api/oven/<oven_id>/alerts")
def oven_alerts_create(oven_id):
    """Creates a new alert scoped to this oven. No auth - matching this
    repo's own existing posture (only /ingest is gated by an API key;
    there's no account system here at all, unlike granco_monitor)."""
    payload = request.get_json(force=True, silent=True) or {}
    doc, err = alerts.build_rule_doc(oven_id, payload)
    if err:
        return jsonify(error=err), 400
    from .db import get_db
    db = get_db()
    result = db.alert_rules.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(_serialize_alert_rule(doc)), 201


@app.patch("/api/oven/<oven_id>/alerts/<rule_id>")
def oven_alerts_update(oven_id, rule_id):
    """Only toggles active - editing triggers or the webhook URL goes
    through delete + re-create instead."""
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload.get("active"), bool):
        return jsonify(error="active must be true/false"), 400
    from .db import get_db
    db = get_db()
    try:
        oid = ObjectId(rule_id)
    except InvalidId:
        return jsonify(error="invalid id"), 400
    result = db.alert_rules.update_one({"_id": oid, "oven_id": oven_id}, {"$set": {"active": payload["active"]}})
    if result.matched_count == 0:
        return jsonify(error="not found"), 404
    return jsonify(ok=True)


@app.delete("/api/oven/<oven_id>/alerts/<rule_id>")
def oven_alerts_delete(oven_id, rule_id):
    from .db import get_db
    db = get_db()
    try:
        oid = ObjectId(rule_id)
    except InvalidId:
        return jsonify(error="invalid id"), 400
    result = db.alert_rules.delete_one({"_id": oid, "oven_id": oven_id})
    if result.deleted_count == 0:
        return jsonify(error="not found"), 404
    return jsonify(ok=True)


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
