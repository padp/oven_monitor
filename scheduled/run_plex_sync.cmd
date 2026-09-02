@echo off
setlocal
REM Sync current Plex loads for every oven that has a plex_workcenter_key.
REM
REM   run_plex_sync.cmd              DB in the default local dir
REM   run_plex_sync.cmd C:\Oven\db   DB somewhere else
REM
REM Uses the shared Plex login one level up from the project root, which is
REM resolved relative to the script - another reason the UNC path below matters.
REM Persistent: the scheduled task starts it at boot and restarts it if it dies.

set "PROJ=\\file1\User\Extrusion DB\Large Oven Uptime Monitoring"

set "OVEN_DB_DIR=%~1"
if "%OVEN_DB_DIR%"=="" set "OVEN_DB_DIR=C:\Oven\db"
if not exist "%OVEN_DB_DIR%" mkdir "%OVEN_DB_DIR%"

set "LOGDIR=%LOCALAPPDATA%\oven_monitor\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "STAMP=%%d"

REM Full UNC script path, local working directory - see the note in
REM run_collector.cmd for why pushd is deliberately not used here.

python -u "%PROJ%\run_plex_sync.py" >> "%LOGDIR%\plex_sync_%STAMP%.log" 2>&1
endlocal
