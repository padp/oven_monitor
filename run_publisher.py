"""Entry point: python run_publisher.py

    python run_publisher.py --check    verify imports and credentials, then exit
"""
import sys


def check():
    from publisher import config
    from publisher.checkpoint import Checkpoint
    print("imports OK")
    print("source db  : %s" % config.COLLECTOR_DB_PATH)
    print("checkpoint : %s" % config.CHECKPOINT_DB_PATH)
    try:
        api_url, api_key = config.load_api_config()
    except (FileNotFoundError, ValueError) as exc:
        print("credentials: NOT CONFIGURED")
        print("  %s" % str(exc).replace("\n", "\n  "))
        return 1
    print("api url    : %s" % api_url)
    print("api key    : %s (%d chars)" % ("*" * 8, len(api_key)))
    cp = Checkpoint()
    for table in ("samples", "state_events"):
        print("last synced: %-13s id %d" % (table, cp.last_id(table)))
    cp.close()
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    from publisher.publisher import run
    run()
