"""Entry point: python run_collector.py

    python run_collector.py --check    verify imports and config, then exit
"""
import sys


def check():
    from collector import config
    from collector.collector import OvenPoller  # noqa: F401
    from collector.detector import Detector     # noqa: F401
    from collector.storage import Storage
    enabled = config.enabled_ovens()
    print("imports OK")
    print("db path  : %s" % config.DB_PATH)
    Storage().close()
    print("database : opened and closed cleanly")
    if not enabled:
        print("WARNING  : no ovens are enabled in collector/config.py")
        return 1
    for oven in enabled:
        print("enabled  : %s at %s (%d tags)" % (oven["name"], oven["ip"], len(oven["tags"])))
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    from collector.collector import run
    run()
