@echo off
setlocal
REM Forward new collector rows from local SQLite to the cloud API.
REM
REM   run_publisher.cmd              DB in the default local dir
REM   run_publisher.cmd C:\Oven\db   DB somewhere else
REM
REM Needs secret\oven_publisher.txt (API_URL / API_KEY) to exist. Persistent:
REM the scheduled task starts it at boot and restarts it if it dies.

set "PROJ=\\file1\User\Extrusion DB\Large Oven Uptime Monitoring"

REM Same local-disk requirement as the collector - this reads the collector's
REM database and keeps its own checkpoint DB alongside it, so both must point
REM at the same local OVEN_DB_DIR.
set "OVEN_DB_DIR=%~1"
if "%OVEN_DB_DIR%"=="" set "OVEN_DB_DIR=C:\Oven\db"
if not exist "%OVEN_DB_DIR%" mkdir "%OVEN_DB_DIR%"

set "LOGDIR=%LOCALAPPDATA%\oven_monitor\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "STAMP=%%d"

REM Full UNC script path, local working directory - see the note in
REM run_collector.cmd for why pushd is deliberately not used here.

python -u "%PROJ%\run_publisher.py" >> "%LOGDIR%\publisher_%STAMP%.log" 2>&1
endlocal
