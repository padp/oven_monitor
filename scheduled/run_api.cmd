@echo off
setlocal
REM Serve the local (SQLite-backed) oven dashboard on port 8000.
REM
REM   run_api.cmd              DB in the default local dir
REM   run_api.cmd C:\Oven\db   DB somewhere else
REM
REM OPTIONAL. The dashboard people actually use is the Mongo-backed cloud API
REM that run_publisher.cmd feeds; this serves the collector's own SQLite file
REM directly and only works on the poller host, where that file lives. Install
REM it with install_tasks.ps1 -WithApi if you want a LAN dashboard that keeps
REM working when the cloud API is unreachable.
REM
REM run_api.py takes no arguments - api/app.py's serve() binds 0.0.0.0:8000.

set "PROJ=\\file1\User\Extrusion DB\Large Oven Uptime Monitoring"

set "OVEN_DB_DIR=%~1"
if "%OVEN_DB_DIR%"=="" set "OVEN_DB_DIR=C:\Oven\db"
if not exist "%OVEN_DB_DIR%" mkdir "%OVEN_DB_DIR%"

set "LOGDIR=%LOCALAPPDATA%\oven_monitor\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "STAMP=%%d"

REM Full UNC script path, local working directory - see the note in
REM run_collector.cmd for why pushd is deliberately not used here.

python -u "%PROJ%\run_api.py" >> "%LOGDIR%\api_%STAMP%.log" 2>&1
endlocal
