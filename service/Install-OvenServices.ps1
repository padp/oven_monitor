<#
.SYNOPSIS
    Installs run_collector.py as an always-on Windows service, wrapped by NSSM.

.DESCRIPTION
    Ported from granco_monitor's Install-GrancoServices.ps1, which encodes a lot
    of hard-won detail about this environment (see .NOTES). Run it on the SAME
    HOST that already runs large_oven_status.py.

    The script is idempotent - re-run it to pick up config changes or a new
    service account password.

    What it does:
      1. Verifies the project path on the share is reachable.
      2. Finds a service-usable Python, or extracts Python's embeddable ZIP to
         local disk and bootstraps pip into it, then installs requirements.txt.
      3. Downloads NSSM (SHA256-verified) to the install root.
      4. Grants "Log on as a service" to the service account.
      5. Creates/reconfigures the service and starts it.

    ONE DIFFERENCE FROM THE SAW PROJECT, deliberate: the SQLite database is put
    on LOCAL DISK (OVEN_DB_DIR, under -InstallRoot) rather than on the share.
    SQLite's locking needs file-lock primitives that SMB only partially
    emulates, which produces real "database is locked" and disk I/O errors. The
    saw project lives with that; there is no reason to inherit it here. A useful
    side effect: the service account then only needs READ on the share, not
    modify.

.PARAMETER ServiceAccount
    Domain account the service runs as, e.g. PAD-WHITEHALL\svc_oven. It needs
    READ on the project share. Use an account whose password does not expire.

.PARAMETER Credential
    Optional. Full credential for ServiceAccount. If omitted you are prompted.
    The password is applied via WMI, never on a command line, so it does not
    leak into process listings.

.PARAMETER ProjectPath
    Where the code lives. Defaults to the share this script was run from.

.PARAMETER InstallRoot
    Local disk location for the Python runtime, nssm.exe, logs and the SQLite
    database. Deliberately NOT on the share.

.EXAMPLE
    # From an ELEVATED PowerShell on the poller host:
    .\Install-OvenServices.ps1 -ServiceAccount 'PAD-WHITEHALL\svc_oven'

.EXAMPLE
    # Re-run later without touching Python or NSSM:
    .\Install-OvenServices.ps1 -ServiceAccount 'PAD-WHITEHALL\svc_oven' -SkipPrereqs

.NOTES
    Uses the Python "embeddable package" ZIP rather than the official Windows
    installer, because the saw project's install found this environment's
    Windows Installer state broken in two separate ways (a corrupt registry key
    under HKLM\...\Installer\Rollback, and an MSI multi-package transaction that
    fails instantly with no logged reason). The embeddable ZIP needs no MSI, no
    installer and no registry writes, so it works regardless.

    This does NOT touch large_oven_status.py. That keeps running as before, and
    the two do not conflict: the legacy poller reads 10.4.20.93 while this
    collector currently reads only 10.4.20.91, and read-only EtherNet/IP
    sessions coexist anyway.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceAccount,

    [System.Management.Automation.PSCredential]$Credential,

    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),

    [string]$InstallRoot = 'C:\OvenMonitor',

    [switch]$SkipPrereqs,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- Pinned downloads -------------------------------------------------------
# Same pins the saw project verified on 2026-07-31.
$PYTHON_EMBED_URL    = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$PYTHON_EMBED_SHA256 = '4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3'
$GET_PIP_URL         = 'https://bootstrap.pypa.io/get-pip.py'
$NSSM_URL            = 'https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip'
$NSSM_SHA256         = '99F5045FFFBFFB745D67FE3A065A953C4A3D9C253B868892D9B685B0EE7D07B8'

$SERVICES = @(
    [pscustomobject]@{
        Name    = 'OvenCollector'
        Script  = 'run_collector.py'
        Display = 'Oven Monitor - PLC Collector'
        Desc    = 'Polls the aging oven PLCs and records samples to oven_monitor.db.'
    }
    [pscustomobject]@{
        Name    = 'OvenPublisher'
        Script  = 'run_publisher.py'
        Display = 'Oven Monitor - API Publisher'
        Desc    = 'Pushes new collector rows from the local SQLite DB up to the cloud API.'
    }
)

# --- Helpers ----------------------------------------------------------------

function Write-Step  { param([string]$m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "    [ok] $m" -ForegroundColor Green }
function Write-Info  { param([string]$m) Write-Host "    $m" -ForegroundColor Gray }
function Write-Warn2 { param([string]$m) Write-Host "    [!] $m" -ForegroundColor Yellow }

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $id
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell (Run as Administrator)."
    }
}

function Get-Verified {
    param([string]$Url, [string]$Sha256, [string]$OutFile)

    if (Test-Path $OutFile) {
        if ((Get-FileHash $OutFile -Algorithm SHA256).Hash -eq $Sha256) {
            Write-Info "already downloaded and verified: $(Split-Path -Leaf $OutFile)"
            return
        }
        Remove-Item $OutFile -Force
    }

    Write-Info "downloading $(Split-Path -Leaf $OutFile) ..."
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        # PS 5.1 can still default to TLS 1.0; add 1.2 without dropping anything newer.
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 600
    } finally {
        $ProgressPreference = $prev
    }

    $actual = (Get-FileHash $OutFile -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
        throw "SHA256 mismatch for $Url`n  expected $Sha256`n  got      $actual"
    }
    Write-Ok "verified $(Split-Path -Leaf $OutFile)"
}

function Find-SystemPython {
    # A service-usable Python: real install, readable by any account.
    # Explicitly rejects the Store build under WindowsApps.
    $candidates = @()
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, 'C:\')) {
        if (-not $root) { continue }
        $candidates += Get-ChildItem -Path $root -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
                       ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }

    $launcher = Join-Path $env:WINDIR 'py.exe'
    if (Test-Path $launcher) {
        try {
            $found = & $launcher -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found.Trim() }
        } catch { }
    }

    foreach ($c in ($candidates | Select-Object -Unique)) {
        if (-not $c -or -not (Test-Path $c)) { continue }
        if ($c -like '*\WindowsApps\*') { continue }   # Store Python - unusable by services
        try {
            $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.9') { return $c }
        } catch { }
    }
    return $null
}

function Grant-LogonAsService {
    param([Parameter(Mandatory = $true)][string]$AccountName)

    $sid = (New-Object Security.Principal.NTAccount($AccountName)).Translate(
               [Security.Principal.SecurityIdentifier]).Value

    $work = Join-Path $env:TEMP "oven-secpol-$PID"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        $export = Join-Path $work 'export.inf'
        $import = Join-Path $work 'import.inf'
        $db     = Join-Path $work 'secedit.sdb'

        & secedit.exe /export /areas USER_RIGHTS /cfg $export | Out-Null
        if (-not (Test-Path $export)) { throw "secedit export failed; cannot grant Log on as a service." }

        $current = Get-Content $export
        $line    = $current | Where-Object { $_ -match '^\s*SeServiceLogonRight\s*=' } | Select-Object -First 1

        if ($line -and $line -match [regex]::Escape($sid)) {
            Write-Info "'Log on as a service' already granted to $AccountName"
            return
        }

        if ($line) {
            $value = ($line -split '=', 2)[1].Trim()
            $value = "$value,*$sid"
        } else {
            $value = "*$sid"
        }

        $inf = @(
            '[Unicode]'
            'Unicode=yes'
            '[Version]'
            'signature="$CHICAGO$"'
            'Revision=1'
            '[Privilege Rights]'
            "SeServiceLogonRight = $value"
        )
        Set-Content -Path $import -Value $inf -Encoding Unicode

        & secedit.exe /configure /db $db /cfg $import /areas USER_RIGHTS | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "secedit /configure returned $LASTEXITCODE" }
        Write-Ok "granted 'Log on as a service' to $AccountName"
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Nssm {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    # NSSM writes its confirmations to stderr. Under $ErrorActionPreference='Stop',
    # PowerShell 5.1 turns 2>&1-redirected native stderr into a terminating
    # NativeCommandError even on exit code 0, so relax it just for this call and
    # judge success by the exit code instead.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $script:NssmExe @Arguments 2>&1
    } finally {
        $ErrorActionPreference = $prevEap
    }

    # NSSM emits UTF-16; strip embedded nulls so the text is readable.
    $text = (($out | ForEach-Object { "$_" }) -join "`n") -replace "`0", ''
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Arguments -join ' ') failed (exit $LASTEXITCODE): $text"
    }
    return $text
}

function Set-ServiceCredential {
    param([string]$ServiceName, [string]$Account, [string]$Password)
    # Applied through WMI so the password never appears in a process command line.
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='$ServiceName'"
    $r = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{
        StartName     = $Account
        StartPassword = $Password
    }
    if ($r.ReturnValue -ne 0) {
        throw "Setting credentials on $ServiceName failed with Win32_Service.Change code $($r.ReturnValue). " +
              "(2=access denied, 15=service marked for deletion, 22=invalid service account, 24=service disabled)"
    }
}

# --- Preflight --------------------------------------------------------------

Assert-Elevated
Write-Host "Oven Monitor - service installer" -ForegroundColor White

Write-Step "Checking the project path"
if (-not (Test-Path $ProjectPath)) { throw "Project path not reachable: $ProjectPath" }
foreach ($s in $SERVICES) {
    $p = Join-Path $ProjectPath $s.Script
    if (-not (Test-Path $p)) { throw "Missing entry point: $p" }
}
Write-Ok "found $($SERVICES.Count) entry point(s) under $ProjectPath"

$dbDir = Join-Path $InstallRoot 'db'
Write-Info "SQLite database will live at $dbDir (local disk, not the share)"

Write-Step "Checking for a conflicting collector instance"
$dbPath = Join-Path $dbDir 'oven_monitor.db'
if (Test-Path $dbPath) {
    $age = (Get-Date) - (Get-Item $dbPath).LastWriteTime
    if ($age.TotalMinutes -lt 5) {
        Write-Warn2 "$dbPath was written $([int]$age.TotalSeconds)s ago - a collector is already running."
        if (-not $Force) {
            $answer = Read-Host "    Continue anyway? (type YES)"
            if ($answer -ne 'YES') { throw "Aborted. Stop the other instance, then re-run." }
        }
    } else {
        Write-Ok "no recent writes to oven_monitor.db"
    }
}

# The legacy poller is expected to be running on this host - that is fine and
# deliberate. Say so explicitly so nobody "helpfully" stops it.
Write-Step "Legacy poller"
Write-Info "large_oven_status.py is expected to keep running on this host, untouched."
Write-Info "It polls 10.4.20.93; this collector currently polls only 10.4.20.91."
Write-Info "Nothing here starts, stops or reconfigures it."

if (-not $Credential) {
    Write-Step "Service account credentials"
    Write-Info "Enter the password for $ServiceAccount"
    $Credential = Get-Credential -UserName $ServiceAccount -Message "Password for the oven service account"
}
$account  = $Credential.UserName
$password = $Credential.GetNetworkCredential().Password
if ([string]::IsNullOrEmpty($password)) { throw "A password is required for the service account." }

Write-Step "Validating credentials against the domain"
# Catching a bad password here saves a confusing "service failed to start" later.
# An unreachable domain is only a warning - the install can still proceed.
$credChecked = $false
$credValid   = $false
try {
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $domain = $Credential.GetNetworkCredential().Domain
    if ([string]::IsNullOrEmpty($domain)) { $domain = $env:USERDOMAIN }
    $ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Domain', $domain)
    $credValid   = $ctx.ValidateCredentials($Credential.GetNetworkCredential().UserName, $password)
    $credChecked = $true
} catch {
    Write-Warn2 "could not reach the domain to pre-validate ($($_.Exception.Message.Split([Environment]::NewLine)[0])); continuing"
}
if ($credChecked) {
    if ($credValid) {
        Write-Ok "credentials accepted by $domain"
    } else {
        throw "The domain rejected the username/password for $account. Re-run and retype the password."
    }
}

# --- Prereqs ----------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$binDir = Join-Path $InstallRoot 'bin'
$logDir = Join-Path $InstallRoot 'logs'
$dlDir  = Join-Path $InstallRoot 'download'
$pyDir  = Join-Path $InstallRoot 'python'
$pyExe  = Join-Path $pyDir 'python.exe'
foreach ($d in @($binDir, $logDir, $dlDir, $dbDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$script:NssmExe = Join-Path $binDir 'nssm.exe'

if (-not $SkipPrereqs) {
    Write-Step "Python"
    $sysPy = Find-SystemPython
    if ($sysPy) {
        $ver = (& $sysPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
        Write-Ok "using existing system Python $ver at $sysPy"
        $pyExe = $sysPy
    } elseif (Test-Path $pyExe) {
        Write-Info "embeddable Python already present at $pyExe"
    } else {
        Write-Info "no service-usable Python found (the Store build under WindowsApps does not count)"
        $zip = Join-Path $dlDir 'python-3.12.10-embed-amd64.zip'
        Get-Verified -Url $PYTHON_EMBED_URL -Sha256 $PYTHON_EMBED_SHA256 -OutFile $zip

        New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
        Expand-Archive -Path $zip -DestinationPath $pyDir -Force
        if (-not (Test-Path $pyExe)) { throw "Extracted the embeddable ZIP but python.exe is missing from $pyDir" }

        # Embeddable Python ships with site-packages processing disabled via a
        # commented-out "import site" line in its ._pth file. Uncommenting it
        # is what makes pip / Lib\site-packages work at all.
        $pth = Get-ChildItem -Path $pyDir -Filter 'python*._pth' | Select-Object -First 1
        if (-not $pth) { throw "Could not find python3*._pth under $pyDir" }
        (Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' |
            Set-Content -Path $pth.FullName
        Write-Ok "extracted Python to $pyDir and enabled site-packages"
    }

    Write-Step "pip"
    $pipMarker = Join-Path $pyDir 'Scripts\pip.exe'
    if ((Test-Path $pipMarker) -or $sysPy) {
        Write-Info "pip already available"
    } else {
        $getPip = Join-Path $dlDir 'get-pip.py'
        # get-pip.py is PyPA's official rolling bootstrap script - by design it
        # has no stable published hash to pin against (unlike Python/NSSM above).
        Write-Info "fetching get-pip.py from bootstrap.pypa.io ..."
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $GET_PIP_URL -OutFile $getPip -UseBasicParsing -TimeoutSec 120
        & $pyExe $getPip --quiet
        if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed (exit $LASTEXITCODE)" }
        Write-Ok "pip installed"
    }

    Write-Step "Dependencies"
    $req = Join-Path $ProjectPath 'requirements.txt'
    & $pyExe -m pip install -r $req --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "pip install -r $req failed (exit $LASTEXITCODE)" }
    $shown = & $pyExe -m pip list --format=freeze --disable-pip-version-check
    Write-Ok "installed:"
    ($shown -split "`n" | Where-Object { $_ -match '^(pylogix)' }) |
        ForEach-Object { Write-Info "  $_" }

    Write-Step "NSSM"
    if (Test-Path $script:NssmExe) {
        Write-Info "nssm.exe already present at $script:NssmExe"
    } else {
        $zip = Join-Path $dlDir 'nssm.zip'
        Get-Verified -Url $NSSM_URL -Sha256 $NSSM_SHA256 -OutFile $zip
        $extract = Join-Path $dlDir 'nssm-extract'
        Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $found = Get-ChildItem -Path $extract -Recurse -Filter 'nssm.exe' |
                 Where-Object { $_.FullName -like '*win64*' } | Select-Object -First 1
        if (-not $found) { throw "win64\nssm.exe not found inside the archive." }
        Copy-Item $found.FullName $script:NssmExe -Force
        Write-Ok "installed $script:NssmExe"
    }
} else {
    Write-Step "Skipping prereqs (-SkipPrereqs)"
    $sysPy = Find-SystemPython
    if ($sysPy) { $pyExe = $sysPy }
    if (-not (Test-Path $pyExe))          { throw "-SkipPrereqs given but Python missing: $pyExe" }
    if (-not (Test-Path $script:NssmExe)) { throw "-SkipPrereqs given but nssm missing: $script:NssmExe" }
}

# --- Preflight: does the code actually import? -------------------------------
# Both entry points support --check: import everything, touch nothing, print
# what they resolved (DB paths, enabled ovens, API credentials). Catching an
# import problem here means a clear message now instead of a service that
# starts and immediately dies with the real cause buried in a log file.

Write-Step "Verifying the code runs under $(Split-Path -Leaf $pyExe)"
Push-Location $ProjectPath
try {
    foreach ($svc in $SERVICES) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & $pyExe -u $svc.Script --check 2>&1 } finally { $ErrorActionPreference = $prevEap }
        $text = ($out | ForEach-Object { "$_" }) -join "`n"
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "$($svc.Script) --check failed (exit $LASTEXITCODE):"
            $text -split "`n" | ForEach-Object { Write-Info "  $_" }
            if ($svc.Name -eq 'OvenPublisher' -and $text -match 'NOT CONFIGURED') {
                throw ("$($svc.Script) has no cloud API credentials yet. Create " +
                       "secret\oven_publisher.txt (see service\README.md), then re-run.")
            }
            throw "$($svc.Script) cannot run under $pyExe. Fix this before installing the service."
        }
        Write-Ok "$($svc.Script)"
        $text -split "`n" | Where-Object { $_ } | ForEach-Object { Write-Info "  $_" }
    }
} finally {
    Pop-Location
}

# --- Permissions ------------------------------------------------------------

Write-Step "Permissions for $account"
Grant-LogonAsService -AccountName $account

$acl = Get-Acl $InstallRoot
$rule = New-Object Security.AccessControl.FileSystemAccessRule(
    $account, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -Path $InstallRoot -AclObject $acl

foreach ($d in @($logDir, $dbDir)) {
    $a = Get-Acl $d
    $r = New-Object Security.AccessControl.FileSystemAccessRule(
        $account, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $a.SetAccessRule($r)
    Set-Acl -Path $d -AclObject $a
}
Write-Ok "read+execute on $InstallRoot, modify on $logDir and $dbDir"

Write-Warn2 "Not checked automatically: $account needs READ on $ProjectPath."
Write-Warn2 "It does NOT need write access there - the database is on local disk."

# --- Services ---------------------------------------------------------------

foreach ($s in $SERVICES) {
    Write-Step "Service $($s.Name)"

    $existing = Get-Service -Name $s.Name -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne 'Stopped') {
            Write-Info "stopping existing service ..."
            Stop-Service -Name $s.Name -Force
            $existing.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
        }
        Invoke-Nssm @('set', $s.Name, 'Application', $pyExe) | Out-Null
        Write-Info "reconfiguring existing service"
    } else {
        Invoke-Nssm @('install', $s.Name, $pyExe) | Out-Null
        Write-Info "created service"
    }

    # AppDirectory is the project root, so the script name needs no path - which
    # keeps the spaces in "Large Oven Uptime Monitoring" out of the argument
    # string entirely.
    Invoke-Nssm @('set', $s.Name, 'AppParameters', "-u $($s.Script)")     | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppDirectory', $ProjectPath)           | Out-Null
    Invoke-Nssm @('set', $s.Name, 'DisplayName', $s.Display)              | Out-Null
    Invoke-Nssm @('set', $s.Name, 'Description', $s.Desc)                 | Out-Null

    # Delayed start: the share and DNS need to be up before the loop connects.
    Invoke-Nssm @('set', $s.Name, 'Start', 'SERVICE_DELAYED_AUTO_START')  | Out-Null
    Invoke-Nssm @('set', $s.Name, 'DependOnService', 'LanmanWorkstation') | Out-Null

    # OVEN_DB_DIR is what keeps SQLite off the SMB share - see the .DESCRIPTION.
    # PYTHONUNBUFFERED plus -u makes log lines appear as they happen rather than
    # in 8 KB bursts.
    Invoke-Nssm @('set', $s.Name, 'AppEnvironmentExtra',
                  "OVEN_DB_DIR=$dbDir", 'PYTHONUNBUFFERED=1', 'PYTHONIOENCODING=utf-8') | Out-Null

    $outLog = Join-Path $logDir "$($s.Name).out.log"
    $errLog = Join-Path $logDir "$($s.Name).err.log"
    Invoke-Nssm @('set', $s.Name, 'AppStdout', $outLog)                   | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppStderr', $errLog)                   | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppStdoutCreationDisposition', '4')    | Out-Null  # append
    Invoke-Nssm @('set', $s.Name, 'AppStderrCreationDisposition', '4')    | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppRotateFiles', '1')                  | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppRotateOnline', '1')                 | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppRotateSeconds', '86400')            | Out-Null  # daily
    Invoke-Nssm @('set', $s.Name, 'AppRotateBytes', '10485760')           | Out-Null  # or 10 MB

    # Restart the Python process whenever it exits, for any reason.
    Invoke-Nssm @('set', $s.Name, 'AppExit', 'Default', 'Restart')        | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppRestartDelay', '15000')             | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppThrottle', '15000')                 | Out-Null

    # On stop, NSSM sends Ctrl+C first - Python turns that into KeyboardInterrupt,
    # which run() already catches, so plc.close()/storage.close() actually run.
    Invoke-Nssm @('set', $s.Name, 'AppStopMethodSkip', '0')               | Out-Null
    Invoke-Nssm @('set', $s.Name, 'AppStopMethodConsole', '15000')        | Out-Null

    Set-ServiceCredential -ServiceName $s.Name -Account $account -Password $password
    Write-Ok "configured, running as $account"

    # SCM-level recovery, in case nssm.exe itself dies rather than the child.
    & sc.exe failure $s.Name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
    & sc.exe failureflag $s.Name 1 | Out-Null
}

# --- Start and verify -------------------------------------------------------

Write-Step "Starting services"
foreach ($s in $SERVICES) {
    try {
        Start-Service -Name $s.Name
        Write-Info "started $($s.Name)"
    } catch {
        Write-Warn2 "could not start $($s.Name): $($_.Exception.Message)"
    }
}

# One poll interval is 30s, so wait past it to confirm a real poll landed.
Write-Info "waiting 45s to confirm the service stays up and polls ..."
Start-Sleep -Seconds 45

Write-Step "Status"
$allGood = $true
foreach ($s in $SERVICES) {
    $svc = Get-Service -Name $s.Name -ErrorAction SilentlyContinue
    $state = 'MISSING'
    if ($svc) { $state = $svc.Status }
    if ($state -eq 'Running') {
        Write-Ok "$($s.Name): Running"
    } else {
        $allGood = $false
        Write-Warn2 "$($s.Name): $state"
        $errLog = Join-Path $logDir "$($s.Name).err.log"
        if (Test-Path $errLog) {
            Write-Info "last lines of $($s.Name).err.log:"
            Get-Content $errLog -Tail 15 | ForEach-Object { Write-Info "  $_" }
        }
    }
}

if (Test-Path $dbPath) {
    $age = (Get-Date) - (Get-Item $dbPath).LastWriteTime
    if ($age.TotalSeconds -lt 90) {
        Write-Ok "database written $([int]$age.TotalSeconds)s ago - the collector is polling"
    } else {
        $allGood = $false
        Write-Warn2 "database has not been written recently - check the logs"
    }
} else {
    $allGood = $false
    Write-Warn2 "no database created yet at $dbPath - check the logs"
}

Write-Host ""
if ($allGood) {
    Write-Host "Collector is running and set to start at boot." -ForegroundColor Green
} else {
    Write-Host "Something is not right - see the log excerpts above." -ForegroundColor Yellow
    Write-Host "Most common cause: the service account lacks READ on $ProjectPath." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Database: $dbPath" -ForegroundColor Gray
Write-Host "Logs:     $logDir" -ForegroundColor Gray
Write-Host "Manage:   .\Oven-Services.ps1 status | start | stop | restart | logs" -ForegroundColor Gray
Write-Host "Tweak:    $script:NssmExe edit OvenCollector" -ForegroundColor Gray
