# Running the collector and publisher as Windows services

Run these **on the same host that already runs `large_oven_status.py`**, from an
elevated PowerShell. Two services are installed:

- **OvenCollector** - polls the PLCs, writes to local SQLite (`run_collector.py`)
- **OvenPublisher** - forwards new rows from that SQLite file to the cloud API
  (`run_publisher.py`)

## Before installing: cloud API credentials

`OvenPublisher` needs `secret\oven_publisher.txt` (plain `KEY=value` lines, same
convention as the sibling projects) to exist **before** you install:

```
API_URL=https://oven-monitor.onrender.com
API_KEY=<same value as the INGEST_API_KEY env var set on Render>
```

The installer's preflight step checks for this and will stop with a clear message
if it is missing, rather than installing a service that starts and immediately dies.

## Install

```powershell
.\Install-OvenServices.ps1 -ServiceAccount 'DOMAIN\svc_oven'
```

Idempotent - re-run it to pick up config changes or a new password. Add
`-SkipPrereqs` to skip the Python/NSSM steps once they are in place.

Before touching the Windows service layer at all, the installer runs both entry
points with `--check`: import everything, touch nothing, print what they resolved
(DB paths, enabled ovens, API credentials). This is what catches a Python problem -
including "no credentials yet" - as a plain message instead of a service that starts
and immediately dies with the real cause buried in a log file.

## Manage

```powershell
.\Oven-Services.ps1 status     # both services' state AND whether it is actually polling
.\Oven-Services.ps1 restart    # restarts both
.\Oven-Services.ps1 logs -Tail 100
.\Oven-Services.ps1 follow     # tail -f the collector log
```

`status` deliberately checks the database's last-write time as well as the service
state. A service can be Running while the poll loop is wedged, and the service state
alone will not show you that. It says nothing about whether the publisher's POSTs are
actually reaching Render - check `logs` for that, or the API's own `/api/health`.

## Uninstall

```powershell
.\Uninstall-OvenServices.ps1              # keeps the database and logs
.\Uninstall-OvenServices.ps1 -RemoveData  # deletes them too
```

## What gets installed

Everything lands under `-InstallRoot` (default `C:\OvenMonitor`), on local disk:

    C:\OvenMonitor\
      bin\nssm.exe            service wrapper
      python\                 embeddable Python, only if no usable system Python exists
      db\oven_monitor.db      the collected data (OvenCollector writes, OvenPublisher reads)
      db\publisher_state.db   OvenPublisher's sync checkpoint - separate file, never
                              written by OvenCollector
      logs\                   rotating stdout/stderr per service, daily or 10 MB

The code itself stays on the share and is run from there.

## Why the database is on local disk

SQLite's locking relies on file-lock primitives that SMB only partially emulates,
which produces real "database is locked" and disk I/O errors. The service sets
`OVEN_DB_DIR` to a local path so the DB never lives on the share.

Useful consequence: the service account needs only **READ** on the project share, not
modify. It does need modify on `C:\OvenMonitor\db` and `\logs`, which the installer
grants.

## Relationship to the legacy poller

`large_oven_status.py` keeps running, untouched. These scripts never start, stop or
reconfigure it. There is no conflict:

- the legacy poller reads `10.4.20.93` (large oven)
- this collector currently reads only `10.4.20.91` (small oven)

and read-only EtherNet/IP sessions coexist fine even when they do overlap, which
matters for the day the large oven is enabled here too.

## Notes carried over from the saw project

- **Embeddable Python ZIP, not the MSI.** This environment's Windows Installer state
  was found broken in two separate ways during the saw project's setup. The
  embeddable ZIP needs no MSI, no installer and no registry writes.
- **The Store Python under `WindowsApps` is rejected.** Services cannot use it.
- **Credentials are applied via WMI**, never on a command line, so the password does
  not appear in process listings.
- **NSSM writes confirmations to stderr.** Under `$ErrorActionPreference='Stop'`,
  PowerShell 5.1 turns redirected native stderr into a terminating
  `NativeCommandError` even on exit code 0 - hence the `Invoke-Nssm` wrapper that
  judges success by exit code.
- **Stop sends Ctrl+C first**, which Python raises as `KeyboardInterrupt` - already
  caught by `run()`, so `plc.close()` and `storage.close()` actually run.
