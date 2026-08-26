"""Main poll loop for the oven collector.

Polls every oven marked enabled in config.OVENS, on one shared interval.
Each oven owns its own PLC socket, detector state and error counter, so a
PLC that goes unreachable is reconnected on its own without interrupting
the other - which matters as soon as the large oven is switched on
alongside the small one.
"""
import time
from datetime import datetime

try:
    from . import config
    from .detector import Detector
    from .plc_client import PlcClient
    from .storage import Storage, summarize_load_temps
except ImportError:
    # Running this file directly (python collector\collector.py) breaks the
    # relative imports above, because the file is then a top-level script
    # rather than part of the `collector` package. The raw error - "attempted
    # relative import with no known parent package" - points at the import
    # line and says nothing about how to fix it, so translate it.
    if __package__ in (None, ""):
        raise SystemExit(
            "collector/collector.py is a package module and cannot be run directly.\n"
            "Run it from the project root instead:\n"
            "    python run_collector.py\n"
            "or, equivalently:\n"
            "    python -m collector.collector"
        )
    raise


class OvenPoller:
    """One oven's connection, detector and error state."""

    def __init__(self, oven):
        self.oven = oven
        self.oven_id = oven["id"]
        self.name = oven["name"]
        self.plc = PlcClient(oven["ip"])
        self.detector = Detector(oven)
        self.consecutive_errors = 0

    def poll(self, ts, storage):
        tag_names = list(self.oven["tags"].keys())
        load_tc_tags = self.oven["load_tc_tags"]

        raw = self.plc.read_all(tag_names + load_tc_tags)
        snapshot = self._to_snapshot(raw)
        load_temps = {t.split(".", 1)[-1]: raw.get(t) for t in load_tc_tags}

        state, reason = self.detector.evaluate(snapshot, summarize_load_temps(load_temps))
        storage.insert_sample(self.oven_id, ts, snapshot, state, reason, load_temps)
        storage.record_state(self.oven_id, ts, state, reason)
        return state, reason

    def _to_snapshot(self, raw):
        """PLC tag names -> canonical field names, with unit scaling.

        Scaling is applied here rather than in the detector so that every
        consumer of a canonical field gets the same units - notably
        cycle_time_left_min, which the small oven reports in hours.
        """
        scales = self.oven["scales"]
        snapshot = {}
        for tag, field in self.oven["tags"].items():
            value = raw.get(tag)
            scale = scales.get(field)
            if scale is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = value * scale
            snapshot[field] = value
        return snapshot

    def reconnect(self):
        try:
            self.plc.close()
        except Exception:
            pass
        self.plc = PlcClient(self.oven["ip"])
        self.consecutive_errors = 0

    def close(self):
        try:
            self.plc.close()
        except Exception:
            pass


def run():
    ovens = config.enabled_ovens()
    if not ovens:
        print("No ovens enabled in config.OVENS - nothing to do.")
        return

    storage = Storage()
    storage.resume_open_states()
    pollers = [OvenPoller(o) for o in ovens]

    for p in pollers:
        print("Polling %s at %s every %gs" % (p.name, p.oven["ip"], config.POLL_INTERVAL_S))
    disabled = [o["name"] for o in config.OVENS.values() if not o["enabled"]]
    if disabled:
        print("Not polling (disabled in config): %s" % ", ".join(disabled))

    last_state = {}
    try:
        while True:
            ts = datetime.now()
            for p in pollers:
                try:
                    state, reason = p.poll(ts, storage)
                    p.consecutive_errors = 0
                    if last_state.get(p.oven_id) != state:
                        print("[%s] %s -> %s (%s)" % (ts.isoformat(timespec="seconds"), p.name, state, reason))
                        last_state[p.oven_id] = state
                except Exception as exc:
                    p.consecutive_errors += 1
                    print("[%s] %s poll error (%d/%d): %s" % (
                        ts.isoformat(timespec="seconds"), p.name,
                        p.consecutive_errors, config.MAX_CONSECUTIVE_ERRORS, exc))
                    if p.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                        print("%s: too many consecutive errors, reconnecting..." % p.name)
                        p.reconnect()
                        last_state.pop(p.oven_id, None)

            storage.prune_samples(ts)
            time.sleep(config.POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping collector.")
    finally:
        for p in pollers:
            p.close()
        storage.close()


if __name__ == "__main__":
    run()
