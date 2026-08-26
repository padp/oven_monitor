"""Configuration for the local->cloud sync job.

Reads cloud API credentials from a local secret/oven_publisher.txt file
(plain KEY=value lines), matching the convention already used by sibling
projects rather than .env/dotenv. That file is not created by setup -
create it after deploying api/ to Render:

    API_URL=https://oven-monitor.onrender.com
    API_KEY=<same value as the INGEST_API_KEY env var set on Render>
"""
import os

from collector.config import DB_PATH as COLLECTOR_DB_PATH  # noqa: F401  (re-exported)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_FILE = os.path.join(_PROJECT_ROOT, "secret", "oven_publisher.txt")

# Same env var as the collector, so the checkpoint DB lands next to
# oven_monitor.db wherever that ends up - which on the poller host is local
# disk, not the share. See collector/config.py for why.
_DB_DIR = os.environ.get("OVEN_DB_DIR", os.path.join(_PROJECT_ROOT, "db"))
CHECKPOINT_DB_PATH = os.path.join(_DB_DIR, "publisher_state.db")

# The collector polls every 30s, so syncing faster than that just produces
# empty ticks. 20s keeps the dashboard within one poll of live without
# hammering a free-tier Render instance.
SYNC_INTERVAL_S = 20
BATCH_LIMIT = 500

# Generous margin for Render free-tier cold starts (a spun-down instance
# can take 30-50s to wake on the first request), on top of /ingest itself.
REQUEST_TIMEOUT_S = 60


def _load_secret_file(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Missing {path}. Create it with two lines:\n"
            "  API_URL=https://oven-monitor.onrender.com\n"
            "  API_KEY=<same value as INGEST_API_KEY on Render>\n"
            "(see api/app.py for the Render env var name)"
        )
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def load_api_config():
    values = _load_secret_file(SECRET_FILE)
    api_url = values.get("API_URL")
    api_key = values.get("API_KEY")
    if not api_url or not api_key:
        raise ValueError(f"{SECRET_FILE} must define both API_URL and API_KEY")
    return api_url.rstrip("/"), api_key
