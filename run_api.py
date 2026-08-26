"""Entry point: python run_api.py

    python run_api.py --check    verify imports and config, then exit

Exists so the service wrapper can launch the API the same way it launches
the collector - a plain script in the project root, which keeps the spaces
in this folder's name out of NSSM's argument string.
"""
import sys


def check():
    from api.app import app          # noqa: F401
    from api import store
    from collector import config
    print("imports OK")
    print("db path  : %s" % config.DB_PATH)
    print("ovens    : %s" % ", ".join(o["id"] for o in store.ovens()))
    try:
        import waitress  # noqa: F401
        print("server   : waitress")
    except ImportError:
        print("server   : Flask dev server (waitress not installed)")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    from api.app import serve
    serve()
