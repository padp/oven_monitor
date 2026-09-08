"""User-configurable Microsoft Teams alerts on either oven's live status.

Vendored from granco_monitor's api/alerts.py (itself ported from picos) -
the trigger/condition schema (bool/numeric/string conditions, compound-AND
evaluation, sustained-duration edge-triggering, recurring/one-time modes,
Teams webhook delivery) is unchanged. Two things are genuinely specific to
this repo: get_current_doc()/list_available_tags() (see their own
docstrings for why this system's shape needs its own adapter), and the
poller needing to walk BOTH ovens each tick instead of checking one
document - every alert_rules document carries an "oven_id" field to scope
it to one oven under this one shared engine/UI.

Each rule posts to a Teams "Incoming Webhook" (or a Workflows app
"Post to a channel when a webhook request is received" flow) URL supplied
when the alert is created - pasted in by whoever owns that Teams channel.
No auth on any endpoint here, matching this repo's own existing posture
(only /ingest is gated, by an API key) - granco_monitor's equivalent
alert-mutation routes require a login instead, because that repo already
has real per-person accounts for other writes (schedule edits) and this
one has none at all, so there's nothing to gate behind.

Trigger schema - one trigger is a compound (AND-only; multiple independent
triggers on one rule already give OR from the user's perspective) list of
conditions that must ALL hold, continuously, for sustained_s before it
fires:
    {
        "id": "<hex>",
        "conditions": [
            {"field": str, "type": "bool", "equals": bool, "mode": "becomes"|"stays"},
            {"field": str, "type": "numeric", "comparator": "<"|"<="|">"|">="|"=="|"!=", "threshold": number},
            {"field": str, "type": "string", "equals": str},
            ...
        ],
        "sustained_s": number,
        "active": bool,
    }

A rule also carries "oven_id" ("small"|"large"), and "repeat": "recurring"
(default) or "one_time" (deleted entirely the first time any trigger fires).
"""
import json
import operator as _op
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from collector import config as collector_config
from . import store
from .db import get_db

REPEAT_MODES = ("recurring", "one_time")

_COMPARATORS = {
    "<": _op.lt,
    "<=": _op.le,
    ">": _op.gt,
    ">=": _op.ge,
    "==": _op.eq,
    "!=": _op.ne,
}

ALLOWED_RECIPIENT_DOMAIN = "uwh.uacj-group.com"

COMPARATOR_PHRASES = {
    "<": "less than",
    "<=": "less than or equal to",
    ">": "greater than",
    ">=": "greater than or equal to",
    "==": "equal to",
    "!=": "not equal to",
}

BOOL_MODES = ("becomes", "stays")

# Matches the data's own refresh rate (collector/config.py's POLL_INTERVAL_S,
# 30s) rather than picos'/granco_monitor's much faster cadence - an oven's
# temperature/cycle state has no reason to be checked for alerting more
# often than the PLC itself is actually polled.
POLL_INTERVAL_S = collector_config.POLL_INTERVAL_S


def plant_now():
    # Every timestamp in this store is UTC (see store_mongo.py's _parse_ts) -
    # matching that here rather than introducing a plant-local timezone this
    # module would otherwise have no reason to know about.
    return datetime.now(timezone.utc)


def comparator_options():
    return [{"value": symbol, "label": phrase} for symbol, phrase in COMPARATOR_PHRASES.items()]


def bool_mode_options():
    return [
        {"value": "becomes", "label": "becomes"},
        {"value": "stays", "label": "stays"},
    ]


def mask_webhook_url(url):
    if not url:
        return None
    tail = url[-6:] if len(url) > 6 else url
    return f"...{tail}"


def get_current_doc(oven_id):
    """One oven's live status as a flat {field: value} dict, for the
    trigger builder/evaluator to work against - built from
    store.current(oven_id)'s own "fields" list (already the merged,
    polarity-noted snapshot every other part of this dashboard reads),
    rather than re-deriving that merge here a second time.

    Returns {} if the oven id is unknown or has never reported a sample
    (store.current returns sample=None in that case) - an empty dict is
    already the correct "nothing to evaluate against yet" input for
    AlertEvaluator.evaluate(), no special-casing needed there.

    Also returns {} when the latest sample is stale (store's own "stale"
    flag - the PLC hasn't reported in longer than ~2.5 poll intervals,
    typically a lost connection) - a frozen last-known reading holding a
    condition true for however long the connection's been down is not the
    same as that condition genuinely holding for real, and treating it as
    real risks a "stays X for N minutes" trigger firing off a connection
    outage rather than an actual sustained oven state. picos/granco_monitor
    don't need this check - their pollers only ever see a document at all
    when the collector is actively writing one - but store.current() here
    keeps returning the last real sample indefinitely, stale flag or not."""
    data = store.current(oven_id)
    if not data or data.get("sample") is None or data.get("stale"):
        return {}
    return {f["field"]: f["value"] for f in data.get("fields", [])}


def list_available_tags(oven_id):
    """Every get_current_doc() field that's a plain bool, number, or
    string. Classified directly here via isinstance rather than trusting
    store.current()'s own per-field "type" label - that classifier (see
    store_mongo.py's _value_type) only distinguishes "bool"/"number"/
    "null"/"other", with no string category at all (a string-valued field
    like "state" comes back "other"), since nothing there needed to
    single out strings before this module existed. This and
    get_current_doc() are the only two oven-specific functions in this
    module - see the module docstring."""
    doc = get_current_doc(oven_id)
    tags = []
    for key, value in doc.items():
        if isinstance(value, bool):
            tags.append({"field": key, "type": "bool"})
        elif isinstance(value, (int, float)):
            tags.append({"field": key, "type": "numeric"})
        elif isinstance(value, str):
            tags.append({"field": key, "type": "string"})
    tags.sort(key=lambda t: t["field"])
    return tags


def _pretty_field(field):
    return re.sub(r"\s*\([^)]*\)\s*$", "", field).strip()


def _format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}m{sec}s" if sec else f"{minutes}m"


def describe_condition(condition):
    pretty = _pretty_field(condition["field"])
    if condition["type"] == "bool":
        word = "stays" if condition.get("mode") == "stays" else "becomes"
        return f"{pretty} {word} {'True' if condition.get('equals', True) else 'False'}"
    if condition["type"] == "string":
        return f'{pretty} is "{condition.get("equals")}"'
    comparator = condition.get("comparator")
    phrase = COMPARATOR_PHRASES.get(comparator, comparator)
    return f"{pretty} {comparator} {condition.get('threshold')} ({phrase})"


def describe_trigger(trigger):
    core = " AND ".join(describe_condition(c) for c in trigger.get("conditions", []))
    sustained_s = trigger.get("sustained_s") or 0
    if sustained_s > 0:
        core += f" for >={_format_duration(sustained_s)}"
    return core


def validate_condition(condition):
    if not isinstance(condition, dict):
        return "each condition must be an object"
    field = condition.get("field")
    if not field or not isinstance(field, str):
        return "each condition needs a field"
    ctype = condition.get("type")
    if ctype not in ("bool", "numeric", "string"):
        return "type must be 'bool', 'numeric', or 'string'"
    if ctype == "bool":
        if not isinstance(condition.get("equals"), bool):
            return "a bool condition needs equals: true/false"
        if condition.get("mode") is not None and condition.get("mode") not in BOOL_MODES:
            return f"mode must be one of {BOOL_MODES}"
    elif ctype == "string":
        equals = condition.get("equals")
        if not isinstance(equals, str) or not equals:
            return "a string condition needs a non-empty equals value"
    else:
        if condition.get("comparator") not in _COMPARATORS:
            return f"comparator must be one of {sorted(_COMPARATORS)}"
        threshold = condition.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            return "a numeric condition needs a numeric threshold"
    return None


def validate_trigger(trigger):
    if not isinstance(trigger, dict):
        return "each trigger must be an object"
    conditions = trigger.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return "each trigger needs at least one condition"
    for condition in conditions:
        err = validate_condition(condition)
        if err:
            return err
    sustained_s = trigger.get("sustained_s", 0)
    if isinstance(sustained_s, bool) or not isinstance(sustained_s, (int, float)) or sustained_s < 0:
        return "sustained_s must be a non-negative number"
    return None


def _build_condition(c):
    condition = {"field": c["field"], "type": c["type"]}
    if c["type"] == "bool":
        condition["equals"] = c["equals"]
        condition["mode"] = c.get("mode") if c.get("mode") in BOOL_MODES else "becomes"
    elif c["type"] == "string":
        condition["equals"] = c["equals"]
    else:
        condition["comparator"] = c["comparator"]
        condition["threshold"] = c["threshold"]
    return condition


def describe_draft_trigger(trigger):
    """Validates a not-yet-created trigger and returns its human-readable
    description, without persisting anything - see granco_monitor's
    identical function for why this exists as a public entry point (a
    natural-language alert-setup tool's confirmation step)."""
    err = validate_trigger(trigger)
    if err:
        return None, err
    built = {
        "conditions": [_build_condition(c) for c in trigger["conditions"]],
        "sustained_s": trigger.get("sustained_s", 0),
    }
    return describe_trigger(built), None


def default_webhook_url():
    import os
    return os.environ.get("DEFAULT_TEAMS_WEBHOOK_URL") or None


def validate_webhook_url(url):
    if not url or not isinstance(url, str):
        if default_webhook_url():
            return None
        return "a Teams webhook URL is required (no default is configured)"
    if not url.startswith("https://"):
        return "the webhook URL must start with https://"
    return None


def validate_recipient_email(email):
    if not email or not isinstance(email, str) or email.count("@") != 1:
        return "a recipient email is required"
    local, _, domain = email.partition("@")
    if not local:
        return "recipient_email doesn't look like a valid email address"
    if domain.lower() != ALLOWED_RECIPIENT_DOMAIN:
        return f"recipient_email must end in @{ALLOWED_RECIPIENT_DOMAIN}"
    return None


def validate_oven_id(oven_id):
    if oven_id not in collector_config.OVENS:
        return f"oven_id must be one of {sorted(collector_config.OVENS)}"
    return None


def build_rule_doc(oven_id, payload):
    """Validates payload (the POST /api/oven/<oven_id>/alerts body) and
    returns (doc, None) or (None, error_message). oven_id comes from the
    URL, not the body - the route it's scoped to is the one source of
    truth for which oven a rule belongs to."""
    if not isinstance(payload, dict):
        return None, "invalid request body"

    err = validate_oven_id(oven_id)
    if err:
        return None, err

    webhook_url = payload.get("webhook_url")
    err = validate_webhook_url(webhook_url)
    if err:
        return None, err
    webhook_url = webhook_url or default_webhook_url()

    recipient_email = payload.get("recipient_email")
    err = validate_recipient_email(recipient_email)
    if err:
        return None, err

    triggers_in = payload.get("triggers")
    if not isinstance(triggers_in, list) or not triggers_in:
        return None, "at least one trigger is required"

    triggers = []
    for t in triggers_in:
        err = validate_trigger(t)
        if err:
            return None, err
        triggers.append({
            "id": secrets.token_hex(4),
            "conditions": [_build_condition(c) for c in t["conditions"]],
            "sustained_s": t.get("sustained_s", 0),
            "active": True,
        })

    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        return None, "label must be a string"

    repeat = payload.get("repeat", "recurring")
    if repeat not in REPEAT_MODES:
        return None, f"repeat must be one of {REPEAT_MODES}"

    return {
        "oven_id": oven_id,
        "label": (label.strip() or None) if label else None,
        "active": True,
        "repeat": repeat,
        "created_at": plant_now().isoformat(),
        "webhook_url": webhook_url,
        "recipient_email": recipient_email,
        "triggers": triggers,
    }, None


def send_teams(webhook_url, title, message, recipient_email=None, timeout=10):
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    body.extend({"type": "TextBlock", "text": line, "wrap": True} for line in message.split("\n") if line)
    payload = {
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ]
    }
    if recipient_email:
        payload["recipient_email"] = recipient_email
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return False, exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        return False, None, str(exc)


def _send_teams_async(webhook_url, title, message, recipient_email=None):
    def _run():
        ok, status, body = send_teams(webhook_url, title, message, recipient_email=recipient_email)
        if not ok:
            print(f"[alerts] Teams send failed ({status}): {body[:200]}")

    threading.Thread(target=_run, daemon=True).start()


class AlertEvaluator:
    """Edge-triggered, same semantics as picos'/granco_monitor's version -
    see either for the full reasoning. Scoped to ONE oven per instance
    (see run_alert_loop, which keeps one evaluator per oven_id so trigger
    state from one oven never bleeds into the other's)."""

    def __init__(self, db, oven_id):
        self._db = db
        self._oven_id = oven_id
        self._trigger_state = {}

    def _single_condition_met(self, condition, doc):
        value = doc.get(condition["field"])
        if value is None:
            return False
        if condition["type"] == "bool":
            return bool(value) == bool(condition.get("equals", True))
        if condition["type"] == "string":
            return str(value) == condition.get("equals")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        cmp = _COMPARATORS.get(condition.get("comparator"))
        if cmp is None:
            return False
        return cmp(value, float(condition.get("threshold", 0)))

    def _condition_met(self, trigger, doc):
        conditions = trigger.get("conditions") or []
        return bool(conditions) and all(self._single_condition_met(c, doc) for c in conditions)

    def evaluate(self, doc):
        if not doc:
            return
        now = plant_now()
        rules = list(self._db.alert_rules.find({"oven_id": self._oven_id, "active": True}))

        live_keys = set()
        for rule in rules:
            if not rule.get("webhook_url"):
                continue
            rule_id = rule["_id"]
            rule_consumed = False
            for trigger in rule.get("triggers", []):
                if rule_consumed:
                    break
                if not trigger.get("active", True):
                    continue
                key = (rule_id, trigger["id"])
                live_keys.add(key)
                state = self._trigger_state.setdefault(key, {"since": None, "fired": False})

                if not self._condition_met(trigger, doc):
                    state["since"] = None
                    state["fired"] = False
                    continue

                if state["since"] is None:
                    state["since"] = now
                held_s = (now - state["since"]).total_seconds()
                sustained_s = trigger.get("sustained_s") or 0
                if held_s >= sustained_s and not state["fired"]:
                    state["fired"] = True
                    self._fire(rule, trigger, doc)
                    if rule.get("repeat") == "one_time":
                        self._db.alert_rules.delete_one({"_id": rule_id})
                        rule_consumed = True

        for key in set(self._trigger_state) - live_keys:
            del self._trigger_state[key]

    def _fire(self, rule, trigger, doc):
        desc = describe_trigger(trigger)
        values = ", ".join(f"{c['field']}={doc.get(c['field'])}" for c in trigger.get("conditions", []))
        oven_name = collector_config.OVENS.get(self._oven_id, {}).get("name", self._oven_id)
        title = rule.get("label") or f"{oven_name} Alert"
        message = f"{desc}\n{values}"
        if rule.get("repeat") == "one_time":
            message += "\n(one-time alert - this rule is now retired)"
        print(f"[alerts] firing {self._oven_id}/{rule['_id']}/{trigger['id']}: {desc} ({values})")
        _send_teams_async(rule["webhook_url"], title, message, recipient_email=rule.get("recipient_email"))


def run_alert_loop():
    db = get_db()
    evaluators = {oven_id: AlertEvaluator(db, oven_id) for oven_id in collector_config.OVENS}
    print(f"[alerts] polling {len(evaluators)} oven(s) for alert rules every {POLL_INTERVAL_S}s")
    while True:
        for oven_id, evaluator in evaluators.items():
            try:
                doc = get_current_doc(oven_id)
                evaluator.evaluate(doc)
            except Exception as exc:
                print(f"[alerts] poll error for {oven_id} (will retry): {exc}")
        time.sleep(POLL_INTERVAL_S)


def start_background_alert_poller():
    threading.Thread(target=run_alert_loop, daemon=True, name="alert-evaluator").start()
