<#
.SYNOPSIS
    Day-to-day management wrapper for the Oven Monitor services.

.EXAMPLE
    .\Oven-Services.ps1 status
    .\Oven-Services.ps1 restart
    .\Oven-Services.ps1 logs -Tail 100
    .\Oven-Services.ps1 follow
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'logs', 'follow')]
    [string]$Action = 'status',

    [string]$InstallRoot = 'C:\OvenMonitor',

    [int]$Tail = 40
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$SERVICE_NAMES = @('OvenCollector', 'OvenPublisher')
$logDir = Join-Path $InstallRoot 'logs'
$dbPath = Join-Path $InstallRoot 'db\oven_monitor.db'

function Write-Ok    { param([string]$m) Write-Host $m -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host $m -ForegroundColor Yellow }
function Write-Info  { param([string]$m) Write-Host $m -ForegroundColor Gray }

function Require-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal $id
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "'$Action' needs an elevated PowerShell (Run as Administrator)."
    }
}

switch ($Action) {

    'status' {
        foreach ($n in $SERVICE_NAMES) {
            $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
            if (-not $svc) { Write-Warn2 "$n : NOT INSTALLED"; continue }
            if ($svc.Status -eq 'Running') { Write-Ok "$n : Running" }
            else { Write-Warn2 "$n : $($svc.Status)" }
        }

        Write-Host ""
        if (Test-Path $dbPath) {
            $age = (Get-Date) - (Get-Item $dbPath).LastWriteTime
            $secs = [int]$age.TotalSeconds
            # The collector polls every 30s, so anything past ~90s means it is
            # up but not actually collecting - a distinction the service state
            # alone will not show you.
            if ($secs -lt 90) { Write-Ok "database written ${secs}s ago - collecting normally" }
            else { Write-Warn2 "database last written ${secs}s ago - service may be up but not polling" }
        } else {
            Write-Warn2 "no database yet at $dbPath"
        }

        Write-Host ""
        Write-Info "Note: large_oven_status.py is a separate legacy poller and is not managed here."
    }

    'start' {
        Require-Elevated
        foreach ($n in $SERVICE_NAMES) { Start-Service -Name $n; Write-Ok "started $n" }
    }

    'stop' {
        Require-Elevated
        foreach ($n in $SERVICE_NAMES) { Stop-Service -Name $n -Force; Write-Ok "stopped $n" }
    }

    'restart' {
        Require-Elevated
        foreach ($n in $SERVICE_NAMES) {
            Restart-Service -Name $n -Force
            Write-Ok "restarted $n"
        }
    }

    'logs' {
        foreach ($n in $SERVICE_NAMES) {
            foreach ($kind in @('out', 'err')) {
                $f = Join-Path $logDir "$n.$kind.log"
                if (-not (Test-Path $f)) { continue }
                Write-Host "`n--- $n.$kind.log (last $Tail) ---" -ForegroundColor Cyan
                Get-Content $f -Tail $Tail
            }
        }
    }

    'follow' {
        $f = Join-Path $logDir "$($SERVICE_NAMES[0]).out.log"
        if (-not (Test-Path $f)) { throw "No log yet at $f" }
        Write-Info "following $f - Ctrl+C to stop"
        Get-Content $f -Tail 20 -Wait
    }
}
