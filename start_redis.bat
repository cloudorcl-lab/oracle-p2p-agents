@echo off
title Redis Server - Oracle P2P Agents
echo Starting Redis server...

set "PROJECT_DIR=%~dp0"

:: Check common locations
for %%R in (
    "%PROJECT_DIR%tools\redis\redis-server.exe"
    "C:\Program Files\Redis\redis-server.exe"
    "C:\Program Files\Memurai\memurai.exe"
) do (
    if exist %%~R (
        echo Found: %%~R
        echo Redis is running. Press Ctrl+C to stop.
        echo.
        %%~R
        goto :eof
    )
)

:: Check recursively in tools dir
for /r "%PROJECT_DIR%tools\redis" %%f in (redis-server.exe) do (
    echo Found: %%f
    echo Redis is running. Press Ctrl+C to stop.
    echo.
    "%%f"
    goto :eof
)

echo Redis server not found. Run setup_env.bat first to install Redis.
pause
