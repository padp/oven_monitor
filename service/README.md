# Running the collector as a Windows service

Run these **on the same host that already runs `large_oven_status.py`**, from an
elevated PowerShell.

## Install

```powershell
.\Install-OvenServices.ps1 -ServiceAccount 'DOMAIN\svc_oven'
```

Idempotent - re-run it to pick up config changes or a new password. Add
`-SkipPrereqs` to skip the Python/NSSM steps once they are in place.

## Manage

```powershell
.\Oven-Services.ps1 status     # service state AND whether it is actually polling
.\Oven-Services.ps1 restart
.\Oven-Services.ps1 logs -Tail 100
.\Oven-Services.ps1 follow     # tail -f the collector log
```

`status` deliberately checks the database's last-write time as well as the service
state. A service can be Running while the poll loop is wedged, and the service state
alone will not show you that.

## Uninstall

```powershell
.\Uninstall-OvenServices.ps1              # keeps the database and logs
.\Uninstall-OvenServices.ps1 -RemoveData  # deletes them too
```

## What gets installed

Everything lands under `-InstallRoot` (default `C:\OvenMonitor`), on local disk:

    C:\OvenMonitor\
      bin\nssm.exe        service wrapper
      python\             embeddable Python, only if no usable system Python exists
      db\oven_monitor.db  the collected data
      logs\               rotating stdout/stderr, daily or 10 MB

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
