@echo off
setlocal
REM Watches connectivity to the large oven's PLC (through the NATR wireless
REM bridge) live, so you can see the exact moment it comes back instead of
REM repeatedly restarting the collector to check. See
REM watch_large_oven_connectivity.ps1's own header for full details.
REM
REM   watch_large_oven_connectivity.cmd
REM   watch_large_oven_connectivity.cmd -BridgeIp 10.4.20.xx
REM
REM Runs until you close the window or press Ctrl+C - this is a continuous
REM watch, not a one-shot report, so there's no pause-at-the-end here.

set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%watch_large_oven_connectivity.ps1" %*
endlocal
