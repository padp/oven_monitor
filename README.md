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

## API and dashboard

    python -m api.app                      # read-only HTTP API on :8000
    cd docs && python -m http.server 8781  # dashboard

Then open `http://localhost:8781/index.html?api=http://localhost:8000`.

`api/` mirrors granco_monitor's shape and will mirror its deployment (Flask on Render,
MongoDB Atlas) once `publisher/` exists. Until then it reads the collector's SQLite
directly via `api/store.py`, so the dashboard is useful now rather than after the whole
cloud pipeline lands. Swapping the backing store later means reimplementing `store.py`
and nothing else - `api/app.py` never sees SQL. There is no `/ingest` yet because
nothing publishes yet.

The dashboard is a **tag monitor**: every canonical field with the PLC tag it came from
and its raw value, grouped and filterable. Above it sits a "Needs attention" panel that
calls out what is actually actionable - collector gone quiet, faults, load probes
reading invalid, and the bits whose polarity is still unconfirmed.

Values are displayed exactly as the PLC reported them. Rows flagged `polarity?` are the
unconfirmed bits, shown raw so a `true` is not mistaken for meaning what the tag name
says. The 24h panel deliberately says "share of observed time" rather than uptime -
the collector has not run long enough, and no full cycle has been watched yet, for a
percentage to mean more than that.

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
