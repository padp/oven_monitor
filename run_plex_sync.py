"""Entry point: python run_plex_sync.py

    python run_plex_sync.py --check    verify imports, config and Plex login, then exit
"""
import sys


def check():
    from collector import config, plex
    from collector.storage import Storage
    ovens = [o for o in config.enabled_ovens() if o.get("plex_workcenter_key")]
    print("imports OK")
    if not ovens:
        print("WARNING: no enabled oven has a plex_workcenter_key set")
        return 1
    for o in ovens:
        print("configured: %s -> WorkcenterKey %s" % (o["name"], o["plex_workcenter_key"]))
    Storage().close()
    print("database: opened and closed cleanly")
    try:
        plex._ensure_logged_in()
    except Exception as exc:
        print("Plex login FAILED: %s" % exc)
        return 1
    print("Plex login: OK (session live or freshly renewed)")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    from collector.plex_sync import run
    run()
