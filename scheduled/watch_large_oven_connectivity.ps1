<#
Watches connectivity to the large oven's PLC (10.4.20.93) AND the 1783-NATR
router it's reached through (10.4.20.94, identified live via RSLinx browsing
2026-09-03 - its private/internal side sits at 10.10.10.1, which is where the
large oven's PLC actually lives before NAT translation makes it appear as
10.4.20.93 on the plant network) - so someone on-site can see the exact
moment either comes back, rather than repeatedly restarting the collector to
check. Pings the small oven too as a working baseline - if all three go down
together, the problem is upstream of the NATR (the plant network itself), not
the NATR/PLC specifically.

    powershell -ExecutionPolicy Bypass -File .\watch_large_oven_connectivity.ps1
    powershell -ExecutionPolicy Bypass -File .\watch_large_oven_connectivity.ps1 -BridgeIp 10.4.20.94

-BridgeIp defaults to the NATR's own address (10.4.20.94) rather than being
blank, since RSLinx already confirmed live that this specific module is the
one failing to respond (shown as "Unrecognized Device" despite RSLinx already
knowing its catalog number - i.e. a real comm failure, not an EDS gap). The
NATR responding but the PLC still not would mean the NATR itself is back but
something behind it (translation table, or the PLC itself) still isn't;
override -BridgeIp "" to go back to only watching the two PLCs directly.

Prints a line only when something's state actually changes (up/down), plus a
"still down" reminder every minute so a multi-hour wait does not look like
the script hung. The moment the large oven's PLC starts responding to ping
again, it also attempts one real EtherNet/IP tag read (Z1_ACTUAL_TEMP) -
ping recovering only proves the network path is back, not that the PLC's CIP
protocol is actually answering through the NATR again, and that gap matters
here specifically since it's a NAT device, not a plain cable.

Everything is also appended to a log file so a recovery that happens while
no one is watching the console is still captured:
    %LOCALAPPDATA%\oven_monitor\logs\connectivity_watch.log

Ctrl+C to stop.
#>
[CmdletBinding()]
param(
  [string]$LargeOvenIp = "10.4.20.93",
  [string]$SmallOvenIp = "10.4.20.91",
  [string]$BridgeIp = "10.4.20.94",
  [int]$IntervalSeconds = 3
)

$logDir = "$env:LOCALAPPDATA\oven_monitor\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir "connectivity_watch.log"

function Write-Line {
  param([string]$Text)
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Text"
  $line
  Add-Content -Path $logPath -Value $line
}

function Test-Reachable {
  param([string]$Ip)
  if (-not $Ip) { return $null }
  return Test-Connection -ComputerName $Ip -Count 1 -Quiet -ErrorAction SilentlyContinue
}

function Test-PlcTagRead {
  # Best-effort: only meaningful if pylogix is installed here. A single
  # scalar read of a tag this project already knows is real (see
  # collector/config.py's LARGE_OVEN_TAGS) - success here means the PLC's
  # CIP protocol is genuinely answering through the bridge, not just ICMP.
  param([string]$Ip)
  $py = @"
import sys
try:
    from pylogix import PLC
except ImportError:
    print('PYLOGIX_NOT_INSTALLED'); sys.exit(0)
with PLC() as plc:
    plc.IPAddress = '$Ip'
    r = plc.Read('Z1_ACTUAL_TEMP')
    if r.Status == 'Success':
        print('PLC_OK value=%s' % r.Value)
    else:
        print('PLC_READ_FAILED status=%s' % r.Status)
"@
  try {
    $result = $py | python - 2>&1
    return ($result | Out-String).Trim()
  } catch {
    return "PLC_READ_ERROR: $_"
  }
}

Write-Line "=== Watching connectivity: large oven $LargeOvenIp (via NATR)$(if ($BridgeIp) { `", NATR itself $BridgeIp`" }), small oven $SmallOvenIp (baseline) ==="
Write-Line "Log: $logPath"

$state = @{}
$downSince = @{}
$lastReminder = @{}
$tracedThisOutage = $false

while ($true) {
  $targets = @{ "large oven ($LargeOvenIp)" = $LargeOvenIp; "small oven ($SmallOvenIp, baseline)" = $SmallOvenIp }
  if ($BridgeIp) { $targets["NATR ($BridgeIp)"] = $BridgeIp }

  foreach ($label in $targets.Keys) {
    $ip = $targets[$label]
    $up = Test-Reachable $ip
    $prev = $state[$label]

    if ($null -eq $prev) {
      Write-Line "$label`: initial state = $(if ($up) { 'UP' } else { 'DOWN' })"
      if (-not $up) {
        $downSince[$label] = Get-Date
        $lastReminder[$label] = Get-Date
        # Trace immediately if it's ALREADY down when the script starts -
        # not just on a later live down-transition. Most real runs of this
        # script start with the large oven already down (that's why it's
        # being run), and that case deserves the same diagnostic as a
        # transition would, not silence until it happens to flip again.
        if ($label -like "large oven*") {
          Write-Line "  running tracert to see where the path breaks ..."
          $tr = tracert -h 15 -w 1000 $ip 2>&1
          $tr | ForEach-Object { Add-Content -Path $logPath -Value "  $_" }
          $tr | ForEach-Object { $_ }
          $tracedThisOutage = $true
        }
      }
    } elseif ($up -and -not $prev) {
      $duration = if ($downSince[$label]) { (Get-Date) - $downSince[$label] } else { $null }
      Write-Line "$label`: RECOVERED$(if ($duration) { " (was down for $($duration.ToString('hh\:mm\:ss')))" })"
      if ($label -like "large oven*") {
        Write-Line "  checking actual PLC tag read (not just ping) ..."
        Write-Line "  $(Test-PlcTagRead $ip)"
        $tracedThisOutage = $false
      }
    } elseif (-not $up -and $prev) {
      Write-Line "$label`: DOWN"
      $downSince[$label] = Get-Date
      $lastReminder[$label] = Get-Date
      if ($label -like "large oven*" -and -not $tracedThisOutage) {
        Write-Line "  running tracert to see where the path breaks ..."
        $tr = tracert -h 15 -w 1000 $ip 2>&1
        $tr | ForEach-Object { Add-Content -Path $logPath -Value "  $_" }
        $tr | ForEach-Object { $_ }
        $tracedThisOutage = $true
      }
    } elseif (-not $up -and -not $prev) {
      if (((Get-Date) - $lastReminder[$label]).TotalSeconds -ge 60) {
        $duration = (Get-Date) - $downSince[$label]
        Write-Line "$label`: still down (elapsed $($duration.ToString('hh\:mm\:ss')))"
        $lastReminder[$label] = Get-Date
      }
    }

    $state[$label] = $up
  }

  Start-Sleep -Seconds $IntervalSeconds
}
