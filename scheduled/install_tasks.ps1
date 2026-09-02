<#
Registers the oven monitoring scheduled tasks. Run it yourself when you are
happy with the settings - it changes Windows configuration, so it is
deliberately a separate, explicit step rather than something the pipeline does
on its own.

    powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
    powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -WithApi
    powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -Remove

Three tasks by default, all PERSISTENT rather than batch - unlike the sibling
Vision System Database's nightly jobs, these processes are meant to stay up:

  OvenCollector  - polls the PLCs, writes state_events to local SQLite
  OvenPublisher  - forwards new rows to the cloud API
  OvenPlexSync   - syncs current Plex loads

So they get no execution time limit, restart-on-failure, and IgnoreNew for
multiple instances (a slow-to-exit previous run is never doubled up). This
mirrors what the top-level README already describes as the real deployment -
Task Scheduler, not the NSSM services in service/, which are kept only as an
unused alternative.

WHEN THEY RUN, and why it is not simply "at startup":

  Default (no switch): -LogonType Interactive + an AT LOGON trigger. The tasks
  run while someone is logged on to the collector host, which is what the
  sibling Vision System Database/scheduled/install_tasks.ps1 does on the same
  kind of box.

  -RunWhenLoggedOff: -LogonType Password + an AT STARTUP trigger. Survives
  reboots with nobody logged on, at the cost of storing the account's password
  in the task (prompted for, never written to this file or the repo). The
  account also needs the "Log on as a batch job" right.

These two are a matched pair and must stay that way. An Interactive principal
with an AtStartup trigger - which is what this script did before - is
incoherent: no interactive session exists at boot, so the task simply never
fires. Pick a logon type and take its trigger with it.

Do NOT "fix" this by reaching for -LogonType S4U, the other "run whether user
is logged on or not" option that avoids storing a password. S4U tokens carry no
NETWORK credentials, and this project launches from \\file1\... - so every task
would fail to reach its own scripts. The password is the price of running
logged off here.

Two things this project is fussy about, both enforced here:

  UNC path, never a mapped drive. Every path in the project derives from
  os.path.abspath(__file__), so whatever path the task launches from is the one
  that propagates to secret/, db/ and the shared Plex credentials. A drive
  letter is tied to the interactive logon that created it and is invisible to a
  task running as a service account or in another session - it fails, or worse
  silently resolves to nothing, even though it works by hand.

  The database on LOCAL disk. SQLite's locking needs file-lock primitives SMB
  only partially emulates; on the share it produces "database is locked" under
  any concurrent access. -DbDir is passed through to each .cmd as OVEN_DB_DIR.

Preflight runs each entry point's --check first (imports everything, touches
nothing, prints what it resolved), the same guard service/Install-OvenServices.ps1
uses. Catching a bad import or missing credentials that way is a lot faster than
watching a task fail silently and digging through Task Scheduler history.
#>
[CmdletBinding()]
param(
  [string]$DbDir = "C:\Oven\db",   # LOCAL disk - never the share
  [switch]$WithApi,                # also register the optional local dashboard
  [switch]$ApiOnly,                # register ONLY the local dashboard
  [switch]$SkipPreflight,          # skip the --check runs
  [switch]$RunWhenLoggedOff,       # at-startup + stored password (see header)
  [string]$User = "$env:USERDOMAIN\$env:USERNAME",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$proj = Split-Path -Parent $here

$collector = Join-Path $here "run_collector.cmd"
$publisher = Join-Path $here "run_publisher.cmd"
$plexSync  = Join-Path $here "run_plex_sync.cmd"
$api       = Join-Path $here "run_api.cmd"

$nameCollector = "OvenCollector"
$namePublisher = "OvenPublisher"
$namePlexSync  = "OvenPlexSync"
$nameApi       = "OvenLocalDashboard"

if ($Remove) {
  foreach ($n in @($nameCollector, $namePublisher, $namePlexSync, $nameApi)) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $n -Confirm:$false
      "removed: $n"
    } else { "not present: $n" }
  }
  return
}

foreach ($f in @($collector, $publisher, $plexSync, $api)) {
  if (-not (Test-Path $f)) { throw "missing $f" }
}

# --- preflight ---------------------------------------------------------
# Run under the SAME account the tasks will run as, and confirm every printed
# path starts with \\file1\... rather than a drive letter.
if (-not $SkipPreflight) {
  $checks = if ($ApiOnly) { @("run_api.py") }
            else { @("run_collector.py", "run_publisher.py", "run_plex_sync.py") +
                   $(if ($WithApi) { @("run_api.py") } else { @() }) }
  $env:OVEN_DB_DIR = $DbDir
  foreach ($script in $checks) {
    "--- $script --check ---"
    # Full UNC path to the script, NOT Push-Location + a bare filename. A child
    # process does not inherit a UNC working directory, so the relative form
    # resolves somewhere else entirely (measured). Handing python the UNC path
    # keeps __file__ UNC and puts the project root on sys.path, which is what
    # makes the collector/ and publisher/ imports work.
    & python "$proj\$script" --check
    if ($LASTEXITCODE -ne 0) {
      throw "$script --check failed (exit $LASTEXITCODE). Fix this before registering the task."
    }
  }
  ""
}

# Logon type and trigger are chosen together - see the header for why pairing
# them wrongly (Interactive + AtStartup) yields a task that never fires.
if ($RunWhenLoggedOff) {
  $cred = Get-Credential -UserName $User `
    -Message "Password for $User. Task Scheduler stores it; this script does not."
  $plainPassword = $cred.GetNetworkCredential().Password
  if (-not $plainPassword) {
    throw "A password is required for -RunWhenLoggedOff. Without one the only " +
          "passwordless alternative is S4U, which cannot reach \\file1\... (see header)."
  }
  "principal: $User, runs whether logged on or not, triggered at startup"
} else {
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited
  "principal: $env:USERNAME, runs while logged on, triggered at logon"
  "  (re-run with -RunWhenLoggedOff to survive reboots with nobody logged on)"
}
""

function Register-Persistent {
  param([string]$Name, [string]$Cmd, [string]$Description)

  # -WorkingDirectory is deliberately local: cmd.exe refuses to start in a UNC
  # directory ("UNC paths are not supported"). It does not need to be the
  # project - each .cmd hands python the script's full UNC path, and that, not
  # the working directory, is what every derived path follows.
  $action = New-ScheduledTaskAction -Execute $Cmd -Argument "`"$DbDir`"" `
    -WorkingDirectory $env:LOCALAPPDATA
  $trigger = if ($RunWhenLoggedOff) { New-ScheduledTaskTrigger -AtStartup }
             else { New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME }

  # ExecutionTimeLimit zero = no limit: these are meant to run indefinitely.
  # IgnoreNew so a restart never doubles up on a still-exiting previous run.
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

  if ($RunWhenLoggedOff) {
    # -User/-Password (rather than -Principal) is how Register-ScheduledTask
    # takes a LogonType Password principal; passing both is an error.
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
      -User $User -Password $plainPassword -RunLevel Limited `
      -Settings $settings -Force -Description $Description | Out-Null
    "registered: $Name  (at startup, OVEN_DB_DIR=$DbDir)"
  } else {
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
      -Principal $principal -Settings $settings -Force -Description $Description | Out-Null
    "registered: $Name  (at logon, OVEN_DB_DIR=$DbDir)"
  }
}

if ($ApiOnly) {
  Register-Persistent $nameApi $api "Serves the local SQLite-backed oven dashboard on port 8000."
  return
}

Register-Persistent $nameCollector $collector `
  "Polls the oven PLCs and writes state_events to local SQLite at $DbDir."
Register-Persistent $namePublisher $publisher `
  "Forwards new collector rows from local SQLite to the cloud API."
Register-Persistent $namePlexSync $plexSync `
  "Syncs current Plex loads for every oven with a plex_workcenter_key."

if ($WithApi) {
  Register-Persistent $nameApi $api "Serves the local SQLite-backed oven dashboard on port 8000."
}

""
if ($RunWhenLoggedOff) {
  "These trigger at startup. To begin now without rebooting:"
} else {
  "These trigger at logon. To begin now without logging out:"
}
"  Start-ScheduledTask -TaskName OvenCollector,OvenPublisher,OvenPlexSync"
""
"Check status any time with:"
"  Get-ScheduledTask 'Oven*' | Get-ScheduledTaskInfo | Format-Table TaskName,LastRunTime,LastTaskResult,NextRunTime"
"Logs: $env:LOCALAPPDATA\oven_monitor\logs"
