<#
.SYNOPSIS
    Removes the Oven Monitor services.

.DESCRIPTION
    Stops and deletes the services. By default it leaves the install root alone,
    so the collected database and the logs survive - removing them is a separate,
    explicit choice via -RemoveData.

    Does not touch large_oven_status.py, which is not managed by these scripts.

.EXAMPLE
    .\Uninstall-OvenServices.ps1
    .\Uninstall-OvenServices.ps1 -RemoveData
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot = 'C:\OvenMonitor',
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$SERVICE_NAMES = @('OvenCollector', 'OvenPublisher')
$nssm = Join-Path $InstallRoot 'bin\nssm.exe'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal $id
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated PowerShell (Run as Administrator)."
}

foreach ($n in $SERVICE_NAMES) {
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if (-not $svc) { Write-Host "$n : not installed" -ForegroundColor Gray; continue }

    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $n -Force
        $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
        Write-Host "$n : stopped" -ForegroundColor Gray
    }

    if (Test-Path $nssm) {
        # NSSM writes confirmations to stderr; judge by exit code, not stream.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $nssm remove $n confirm 2>&1 | Out-Null } finally { $ErrorActionPreference = $prev }
    } else {
        & sc.exe delete $n | Out-Null
    }
    Write-Host "$n : removed" -ForegroundColor Green
}

if ($RemoveData) {
    if ($PSCmdlet.ShouldProcess($InstallRoot, "Delete install root including the collected database and logs")) {
        Remove-Item $InstallRoot -Recurse -Force
        Write-Host "removed $InstallRoot" -ForegroundColor Yellow
    }
} else {
    Write-Host ""
    Write-Host "Left in place: $InstallRoot (database and logs)." -ForegroundColor Gray
    Write-Host "Pass -RemoveData to delete it as well." -ForegroundColor Gray
}
