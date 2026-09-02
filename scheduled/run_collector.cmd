@echo off
setlocal
REM Poll the oven PLCs and write state_events to local SQLite.
REM
REM   run_collector.cmd              DB in the default local dir
REM   run_collector.cmd C:\Oven\db   DB somewhere else
REM
REM Persistent: this does not exit on its own. The scheduled task starts it at
REM boot and restarts it if it dies - see install_tasks.ps1.

set "PROJ=\\file1\User\Extrusion DB\Large Oven Uptime Monitoring"

REM The DB must be on LOCAL disk, never the share. SQLite's locking needs
REM file-lock primitives SMB only partially emulates, and pointing it at a UNC
REM path produces "database is locked" under any concurrent access (see the
REM testing note in the top-level README). OVEN_DB_DIR is read by both
REM collector/config.py and publisher/config.py.
set "OVEN_DB_DIR=%~1"
if "%OVEN_DB_DIR%"=="" set "OVEN_DB_DIR=C:\Oven\db"
if not exist "%OVEN_DB_DIR%" mkdir "%OVEN_DB_DIR%"

set "LOGDIR=%LOCALAPPDATA%\oven_monitor\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "STAMP=%%d"

REM Python is handed the script's FULL UNC path, and the working directory is
REM left local on purpose. Do NOT pushd to the project first: pushd does not
REM set the working directory to the UNC path, it quietly maps a temporary
REM drive letter and uses that instead (measured - it produced
REM "S:\Extrusion DB\Large Oven Uptime Monitoring"). Every path in this project
REM derives from os.path.abspath(__file__), so that letter would then propagate
REM into logs, the checkpoint DB and anything else that records a path - and a
REM drive letter means nothing to another session, which is exactly what the
REM top-level README warns about. Passing the UNC path directly keeps __file__
REM UNC, and python puts the script's own directory on sys.path so the
REM collector/ and publisher/ packages still import.
REM -u so a traceback reaches the log immediately; redirected stdout is
REM otherwise buffered and a failed start looks silent.

python -u "%PROJ%\run_collector.py" >> "%LOGDIR%\collector_%STAMP%.log" 2>&1
endlocal
