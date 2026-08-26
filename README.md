# Oven Monitor

Uptime and cycle monitoring for the plant's two aging ovens.

| Oven | PLC | Status |
|------|-----|--------|
| **Large** oven | `10.4.20.93` | Monitored since 2025-10-24 by the legacy poller (below). Tag map proven. |
| **Small** oven | `10.4.20.91` | Not monitored yet. Tag map now discovered - see `reference/oven_91_tag_map.md`. |

## Current state

This repo is mid-transition from a single-oven, file-based poller into a full
two-oven monitoring system mirroring the architecture of the sibling
`granco_monitor` project (collector -> local SQLite -> publisher -> cloud API -> dashboard).

### Legacy poller (still running, do not disturb)

`large_oven_status.py` polls `10.4.20.93` every 30s and appends status snapshots to
daily `Large_Oven_Status_YYYY-MM-DD.json` files. It has run continuously since
2025-10-24 and is **still running today**, hosted on another machine. It stays running
untouched as a redundant safety net; the new collector polls independently
(read-only pollers coexist fine on EtherNet/IP).

`generate_large_oven_report.py` produces on-demand Excel utilization reports into `Reports/`.

The daily JSON files are gitignored - ~500MB and growing, generated data rather than
source. They stay on disk and are the backfill source for the cloud database.

Each snapshot looks like:

```json
{
  "timestamp": "2026-08-26T08:25:28.357522",
  "status": { "state": "RUNNING", "reason": "Oven running cycle - 266 minutes remaining, ..." },
  "indicators": { "zone1_temp": "339", "zone2_temp": "339", "setpoint": "340",
                  "zone1_burner": 44, "zone2_burner": 50, "cycle_time_left_min": 266,
                  "exhaust_fan_active": 65, "auto_mode_selected": true, "manual_mode_selected": false },
  "safety_status": { "z1_safeguard_relay": true, "z2_safeguard_relay": true,
                     "combustion_fault_lock": false, "fault_lock": false,
                     "purge_fault": 0, "power_feed": true }
}
```

## New collector

`python run_collector.py` polls every oven marked `enabled` in
`collector/config.py` and writes to SQLite (`db/oven_monitor.db`).

Currently **only the small oven is enabled**, to establish its pattern before the
large oven is switched over. The large oven stays covered by the legacy poller in the
meantime, so nothing is unmonitored.

    collector/config.py      oven registry + per-oven tag maps
    collector/plc_client.py  thin pylogix wrapper, one socket per oven
    collector/detector.py    RUNNING / IDLE / FAULT / UNKNOWN
    collector/storage.py     SQLite: samples + state_events

Both ovens map their own PLC tag names onto one set of **canonical field names**, so
the detector and storage never learn that the two ovens name things differently.
Every row carries `oven_id`; enabling the large oven is a config change, not a schema
change.

Set `OVEN_DB_DIR` to keep the SQLite file off the network share - SMB only partially
emulates the file-lock primitives SQLite needs.

### Everything is stored raw

Samples record what the PLC actually reported - no polarity correction, no substituting
"sensible" values. Interpretation happens at read time. This matters because two
things about the small oven are still unsettled (bit polarity, and the exact shape of a
real cycle), and a wrong assumption baked into storage would corrupt the very history
that would prove it wrong.

The one exception is the load-temperature aggregate, which excludes probes pinned at
2192 F - that is type J full scale, i.e. "no signal", and averaging it in would drag the
result up by hundreds of degrees. The raw vector is still stored alongside, and
`load_temp_valid_count` records how many probes actually contributed.

### Verification

`collector/detector.py` is a port of `large_oven_status.py`'s `determine_state()`.
Replaying all 293 legacy daily files - **691,379 snapshots** - through the new detector
reproduces the legacy poller's own recorded state on **100.000%** of them.

Four of those files (2025-10-28, 2026-01-16, 2026-05-08, 2026-07-02) are truncated
mid-write, where the legacy poller died partway through the day. A strict JSON parse
rejects the whole file, though the records before the break are recoverable. This is a
direct argument for the new collector's row-at-a-time SQLite commits.

## Pipeline

Mirrors granco_monitor's shape end to end: `collector` writes local SQLite,
`publisher` forwards new rows to a cloud API, `api` (Flask + MongoDB Atlas, on
Render) is the single source of truth, `docs` is a static dashboard reading
from it. Every module also has a local-only path, so each piece is testable
without the others already deployed - see below.

    collector/  ->  db/oven_monitor.db  ->  publisher/  ->  api/ (Render)  ->  docs/

`api/store.py` picks its backend (`store_sqlite` locally, `store_mongo` when
`SQL_PASS` is set) so `api/app.py` never learns which is in play - the same
four functions, the same shapes, either way.

### Running locally (no cloud needed)

    python -m api.app                      # read-only HTTP API on :8000, SQLite-backed
    cd docs && python -m http.server 8781  # dashboard

Then open `http://localhost:8781/index.html?api=http://localhost:8000`.

The dashboard is a **tag monitor**: every canonical field with the PLC tag it came from
and its raw value, grouped and filterable. Above it sits a "Needs attention" panel that
calls out what is actually actionable - collector gone quiet, faults, load probes
reading invalid, and the bits whose polarity is still unconfirmed.

Values are displayed exactly as the PLC reported them. Rows flagged `polarity?` are the
unconfirmed bits, shown raw so a `true` is not mistaken for meaning what the tag name
says. The 24h panel deliberately says "share of observed time" rather than uptime -
the collector has not run long enough, and no full cycle has been watched yet, for a
percentage to mean more than that.

### Deploying the cloud API (one-time, manual)

1. On Render, create a new Web Service from this repo. **Build command must be set
   explicitly** to `pip install -r api/requirements.txt` - Render's zero-config Python
   default is `pip install -r requirements.txt` at the repo root, which is the
   collector's dependencies (`pylogix`, `requests`), not the API's. Leaving the build
   command unset installs the wrong file, gunicorn is never installed, and every route
   502s. Start command is already in `Procfile` and needs no change.
2. Set env vars: `SQL_PASS` (the Atlas cluster password) and `INGEST_API_KEY`
   (any random string - the publisher must send the same value).
3. Confirm `https://oven-monitor.onrender.com/api/health` returns
   `{"ok": true, "backend": "mongo"}`. If Render assigns a different hostname,
   update `API_DEFAULT` in `docs/app.js`.
4. On the poller host, create `secret/oven_publisher.txt`:

       API_URL=https://oven-monitor.onrender.com
       API_KEY=<same value as INGEST_API_KEY>

5. `docs/` deploys to GitHub Pages the same way granco_monitor's does (repo
   Settings -> Pages -> serve from `/docs` on `main`).

### Running it for real

Both `run_collector.py` and `run_publisher.py` run persistently on the same host as
the legacy poller, via **Windows Task Scheduler** - not as Windows services. Each is
a scheduled task running `python.exe run_collector.py` / `python.exe run_publisher.py`
with the working directory set to the project root, triggered at startup (and/or on a
"repeat indefinitely" schedule), with "if the task is already running, do not start a
new instance" set so a slow-to-exit previous run is never doubled up.

Before wiring either up as a scheduled task, run its `--check` flag by hand once -
imports everything, touches nothing, and prints what it resolved (DB path, enabled
ovens, and for the publisher, whether `secret/oven_publisher.txt` is present and
valid):

    python run_collector.py --check
    python run_publisher.py --check

This is the same check the (unused) `service/` installer runs automatically -
catching a bad import or missing credentials this way is a lot faster than watching a
scheduled task fail silently and digging through Task Scheduler's history to find out
why.

The database should live on **local disk**, not the share - SQLite's locking needs
file-lock primitives SMB only partially emulates. Set `OVEN_DB_DIR` in the scheduled
task's environment (or in the task's action, e.g. `cmd /c "set OVEN_DB_DIR=C:\Oven\db && python run_collector.py"`)
to a local path.

**Point the scheduled task at the project by its full UNC path, never a mapped drive
letter** (`\\file1\User\Extrusion DB\Large Oven Uptime Monitoring`, not `T:\...`). Both
scripts derive every other path - `secret/oven_publisher.txt`, the checkpoint DB, the
default `db/` location - from wherever they were actually launched from
(`os.path.abspath(__file__)`), so whatever path style the task's Action uses is the one
that propagates everywhere. Mapped drives are tied to the interactive logon session
that created them; a task running as a service account, "whether user is logged on or
not," or in a different session simply does not see that drive letter, and the task
fails (or worse, silently resolves to nothing) even though it works fine when you test
it by hand while logged in. Set the Action's Program/Script and Start-in fields to the
UNC path, e.g.:

    Program/script:  python.exe
    Arguments:        \\file1\User\Extrusion DB\Large Oven Uptime Monitoring\run_publisher.py
    Start in:         \\file1\User\Extrusion DB\Large Oven Uptime Monitoring

After changing this, re-run `--check` under the same account the task will run as and
confirm every printed path starts with `\\file1\...`, not `T:\...`.

`service/` (NSSM-based Windows services, mirroring granco_monitor's approach) is kept
in the repo but **not used** for this project - Task Scheduler is the actual
deployment method. See [`service/README.md`](service/README.md) only if that ever
needs to change.

### /ingest and idempotency

`state_events` rows are mutated after insert - `ts_end` fills in once a segment closes -
which a plain "id > last synced" sweep would miss: it would publish the open segment
once with `ts_end: null` and never correct it. The publisher instead re-sends any
still-open segment every tick and only advances its checkpoint past the oldest
currently-open one. `/ingest` upserts everything by `source_id` (`oven_id:timestamp`,
not the local SQLite row id - stable across a rebuilt local database), so re-delivery
of an already-synced row is harmless.

## Oven state model

`OvenState` is RUNNING / IDLE / FAULT / UNKNOWN. `determine_state()` in
`large_oven_status.py` encodes the operational knowledge validated over 10+ months of
production logging - fault-lock tags take priority, then burner/exhaust activity.
Carry this reasoning forward rather than reinventing it.

## Tag map - `10.4.20.93` (proven)

Flat scalar tags, taken from `large_oven_status.py`'s `monitoring_tags`:

- **Temps**: `Z1_ACTUAL_TEMP`, `Z2_ACTUAL_TEMP`, `OVEN_TEMP_SETPOINT`
- **Burners**: `ZONE_1_BURNER_MTR`, `ZONE_2_BURNER_MTR`
- **Cycle**: `CYCLE_TOTAL_MINUTES_LEFT`, `SOAK_CYCLE_COMPLETE_COUNTER`
- **Mode**: `AUTOMATIC_MODE_SELECTED`, `MANUAL_MODE_SELECTED`
- **Faults (primary)**: `CombustionFaultLock`, `Fault_Lock`, `PURGE_FAULT`
- **Safety relays (reference only)**: `z1_safeguard_relay`, `z2_safeguard_relay`
- **Exhaust / power**: `EXHAUST_FAN`, `POWER_FEED`
- **Diagnostics**: `FAULTS`, `FAULT_BITS`, `Z1_FLAME_FLAME_FAULT_CTR`, `Z2_FLAME_FLAME_FAULT_CTR`

## Tag map - `10.4.20.91` (incomplete)

Different naming convention from `.93` - UDT-heavy. `BURNER_1`, `BURNER_2`, `OVEN`,
`OVEN_LOAD_CYCLE`, `OVEN_SOAK_TIMER`, `OVEN_RECIPE*` read back as opaque structured
byte blobs via a plain `Read()` and need member-level paths. Only confirmed clean
scalar so far: `BURNER_CONTROL_TEMPERATURE`.

Now mapped - see [`reference/oven_91_tag_map.md`](reference/oven_91_tag_map.md) for the
full map, including two findings that must be respected before ingesting anything:
the `LOAD_TEMP_TO_MMI` probe is failed (pinned at type-J full scale) and the real load
temperatures come from `OVEN_AUX_THERMOCOUPLE`'s eleven probes; and several
permissive/limit booleans appear to be wired normally-closed, so their names read
backwards.

## Conventions

- Local secrets live in `secret/*.txt` (gitignored), matching sibling projects.
- Metrics that report "how often X happened this week" denominate against **5 weekdays
  (Mon-Fri)**, not 7 - the plant doesn't normally run weekends. Surface weekend runs
  separately as overtime.
