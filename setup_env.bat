@echo off
setlocal EnableDelayedExpansion
title Oracle P2P Agents - Environment Setup
color 0A

set "PROJECT_DIR=%~dp0"
set "AGENTS_SRC=%PROJECT_DIR%agents\src"
set "LOGFILE=%PROJECT_DIR%setup_log.txt"
set "ERRORS=0"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

:: Clear and start log
echo Setup started: %DATE% %TIME% > "%LOGFILE%"
echo. >> "%LOGFILE%"

call :log "============================================================"
call :log "  Oracle P2P Agents - Full Environment Setup"
call :log "============================================================"
call :log ""

:: ============================================================
:: 1. FIND PYTHON
:: ============================================================
call :log "[1/6] Checking Python..."

where python >nul 2>&1
if !ERRORLEVEL!==0 (
    for /f "tokens=*" %%i in ('where python') do set "PYTHON=%%i" & goto :found_python
)

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%~P (
        set "PYTHON=%%~P"
        goto :found_python
    )
)

call :log "[FAIL] Python 3.11+ not found."
set /a ERRORS+=1
goto :skip_python

:found_python
for /f "tokens=*" %%v in ('"%PYTHON%" --version 2^>^&1') do set "PYVER=%%v"
call :log "[OK]   %PYVER% at %PYTHON%"

:: ============================================================
:: 2. INSTALL PYTHON PACKAGES
:: ============================================================
call :log ""
call :log "[2/6] Installing Python packages..."

call :log "       Upgrading pip..."
"%PYTHON%" -m pip install --upgrade pip >> "%LOGFILE%" 2>&1

call :log "       Installing requirements.txt..."
"%PYTHON%" -m pip install -r "%PROJECT_DIR%requirements.txt" >> "%LOGFILE%" 2>&1
set "PIP_RC=!ERRORLEVEL!"

call :log "       Installing respx..."
"%PYTHON%" -m pip install respx >> "%LOGFILE%" 2>&1

call :log "       Verifying imports..."
"%PYTHON%" -c "import httpx, pydantic, redis, dotenv; print('All packages importable')" >> "%LOGFILE%" 2>&1
if !ERRORLEVEL!==0 (
    call :log "[OK]   Python packages installed and verified"
) else (
    call :log "[FAIL] Package imports failed (pip exit code was: %PIP_RC%)"
    set /a ERRORS+=1
)

:skip_python

:: ============================================================
:: 3. INSTALL REDIS
:: ============================================================
call :log ""
call :log "[3/6] Checking Redis..."

set "REDIS_DIR=%PROJECT_DIR%tools\redis"
set "REDIS_CLI="
set "REDIS_SERVER="

for %%R in (
    "C:\Program Files\Redis\redis-cli.exe"
    "C:\Program Files\Memurai\memurai-cli.exe"
    "%REDIS_DIR%\redis-cli.exe"
) do (
    if exist %%~R (
        set "REDIS_CLI=%%~R"
        call :log "       Found at known path: %%~R"
        goto :found_redis_cli
    )
)

if exist "%REDIS_DIR%" (
    for /r "%REDIS_DIR%" %%f in (redis-cli.exe) do (
        set "REDIS_CLI=%%f"
        call :log "       Found in tools dir: %%f"
        goto :found_redis_cli
    )
)

call :log "       Redis not found. Downloading..."
if not exist "%REDIS_DIR%" mkdir "%REDIS_DIR%"

set "REDIS_ZIP=%REDIS_DIR%\redis.zip"
set "REDIS_URL=https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"

:: Try curl first
call :log "       Checking curl..."
where curl >> "%LOGFILE%" 2>&1
if !ERRORLEVEL!==0 (
    call :log "       Downloading with curl..."
    curl -L -o "%REDIS_ZIP%" "%REDIS_URL%" >> "%LOGFILE%" 2>&1
    call :log "       curl exit code: !ERRORLEVEL!"
    if exist "%REDIS_ZIP%" goto :extract_redis
)

:: Try PowerShell
call :log "       Checking PowerShell at: %PS%"
if exist "%PS%" (
    call :log "       Downloading with PowerShell..."
    "%PS%" -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%REDIS_URL%' -OutFile '%REDIS_ZIP%'" >> "%LOGFILE%" 2>&1
    call :log "       PowerShell exit code: !ERRORLEVEL!"
    if exist "%REDIS_ZIP%" goto :extract_redis
) else (
    call :log "       PowerShell not found at %PS%"
)

:: Try Python
if defined PYTHON (
    call :log "       Downloading with Python urllib..."
    "%PYTHON%" -c "import urllib.request; urllib.request.urlretrieve('%REDIS_URL%', r'%REDIS_ZIP%'); print('Download complete')" >> "%LOGFILE%" 2>&1
    call :log "       Python download exit code: !ERRORLEVEL!"
    if exist "%REDIS_ZIP%" goto :extract_redis
)

call :log "[FAIL] All download methods failed. Install Redis manually."
call :log "       URL: https://github.com/tporadowski/redis/releases"
set /a ERRORS+=1
goto :skip_redis

:extract_redis
call :log "       Download OK. Extracting..."

if exist "%PS%" (
    call :log "       Extracting with PowerShell..."
    "%PS%" -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%REDIS_ZIP%' -DestinationPath '%REDIS_DIR%' -Force" >> "%LOGFILE%" 2>&1
    call :log "       PowerShell extract exit code: !ERRORLEVEL!"
) else if defined PYTHON (
    call :log "       Extracting with Python zipfile..."
    "%PYTHON%" -c "import zipfile; zipfile.ZipFile(r'%REDIS_ZIP%').extractall(r'%REDIS_DIR%'); print('Extracted')" >> "%LOGFILE%" 2>&1
    call :log "       Python extract exit code: !ERRORLEVEL!"
) else (
    call :log "[FAIL] No tool available to extract zip."
    set /a ERRORS+=1
    goto :skip_redis
)

del "%REDIS_ZIP%" >nul 2>&1

for /r "%REDIS_DIR%" %%f in (redis-cli.exe) do set "REDIS_CLI=%%f"
for /r "%REDIS_DIR%" %%f in (redis-server.exe) do set "REDIS_SERVER=%%f"

if not defined REDIS_CLI (
    call :log "[FAIL] redis-cli.exe not found after extraction."
    call :log "       Listing tools\redis contents:"
    dir /s /b "%REDIS_DIR%" >> "%LOGFILE%" 2>&1
    set /a ERRORS+=1
    goto :skip_redis
)

call :log "[OK]   Redis extracted to %REDIS_DIR%"

:found_redis_cli
call :log "       Redis CLI: %REDIS_CLI%"

if not defined REDIS_SERVER (
    for %%d in ("%REDIS_CLI%") do (
        if exist "%%~dpredis-server.exe" set "REDIS_SERVER=%%~dpredis-server.exe"
        if exist "%%~dpmemurai.exe" set "REDIS_SERVER=%%~dpmemurai.exe"
    )
)
if defined REDIS_SERVER call :log "       Redis Server: %REDIS_SERVER%"

"%REDIS_CLI%" ping >nul 2>&1
if !ERRORLEVEL!==0 (
    call :log "[OK]   Redis is already running (PONG received)"
) else (
    if defined REDIS_SERVER (
        call :log "       Starting Redis server..."
        start "" "%REDIS_SERVER%"
        call :log "       Waiting 3 seconds..."
        if defined PYTHON (
            "%PYTHON%" -c "import time; time.sleep(3)" >nul 2>&1
        ) else (
            ping -n 4 127.0.0.1 >nul 2>&1
        )
        "%REDIS_CLI%" ping >nul 2>&1
        if !ERRORLEVEL!==0 (
            call :log "[OK]   Redis started successfully (PONG received)"
        ) else (
            call :log "[WARN] Redis did not respond after start. Run start_redis.bat manually."
        )
    ) else (
        call :log "[WARN] redis-cli found but no redis-server.exe nearby."
    )
)

:skip_redis

:: ============================================================
:: 4. CREATE .env FILE
:: ============================================================
call :log ""
call :log "[4/6] Checking .env file..."

if exist "%AGENTS_SRC%\.env" (
    call :log "[OK]   .env exists at %AGENTS_SRC%\.env"
) else (
    call :log "       Creating .env..."
    (
        echo # Oracle Fusion Cloud - Basic Auth
        echo ORACLE_HOST=https://fa-eqih-dev20-saasfademo1.ds-fa.oraclepdemos.com/
        echo ORACLE_USERNAME=calvin.roth
        echo ORACLE_PASSWORD=CHANGE_ME
        echo.
        echo # Redis
        echo REDIS_URL=redis://localhost:6379/0
        echo REDIS_HOST=localhost
        echo REDIS_PORT=6379
        echo REDIS_DB=0
    ) > "%AGENTS_SRC%\.env"
    call :log "[OK]   .env created. Edit ORACLE_PASSWORD before running agents."
)

:: ============================================================
:: 5. VERIFY PYTHON IMPORTS
:: ============================================================
call :log ""
call :log "[5/6] Verifying agent imports..."

if defined PYTHON (
    cd /d "%AGENTS_SRC%"
    "%PYTHON%" -c "from agents.pr1_supplier import PR1SupplierAgent; from agents.pr2_requisition import PR2RequisitionAgent; from agents.pr3_sourcing import PR3SourcingAgent; from agents.pr4_agreement import PR4AgreementAgent; from agents.pr5_purchase_order import PR5PurchaseOrderAgent; from agents.pr6_receiving import PR6ReceivingAgent; from agents.pr7_monitor import PR7LifecycleMonitor; print('All 7 agents import OK')" >> "%LOGFILE%" 2>&1
    if !ERRORLEVEL!==0 (
        call :log "[OK]   All 7 agents import successfully"
    ) else (
        call :log "[FAIL] Agent import errors (see details above in log)"
        set /a ERRORS+=1
    )
    cd /d "%PROJECT_DIR%"
) else (
    call :log "[SKIP] No Python"
)

:: ============================================================
:: 6. RUN UNIT TESTS
:: ============================================================
call :log ""
call :log "[6/6] Running unit tests..."

if defined PYTHON (
    cd /d "%AGENTS_SRC%"
    call :log "       Running: python -m unittest discover..."
    "%PYTHON%" -m unittest discover -s tests -p "test_*.py" >> "%LOGFILE%" 2>&1
    set "TEST_RC=!ERRORLEVEL!"
    if !TEST_RC!==0 (
        call :log "[OK]   All tests passed"
    ) else (
        call :log "[WARN] Tests exited with code !TEST_RC! (see details in log)"
    )
    cd /d "%PROJECT_DIR%"
) else (
    call :log "[SKIP] No Python"
)

:: ============================================================
:: SUMMARY
:: ============================================================
call :log ""
call :log "============================================================"
call :log "  SETUP SUMMARY"
call :log "============================================================"
if defined PYTHON call :log "  Python:      %PYVER%"
if defined REDIS_CLI call :log "  Redis CLI:   %REDIS_CLI%"
if defined REDIS_SERVER call :log "  Redis Srv:   %REDIS_SERVER%"
if exist "%AGENTS_SRC%\.env" call :log "  .env:        %AGENTS_SRC%\.env"
call :log ""
if !ERRORS!==0 (
    call :log "  STATUS: ALL GOOD"
) else (
    call :log "  STATUS: !ERRORS! error(s) - see log for details"
)
call :log "============================================================"
call :log ""
call :log "Log written to: %LOGFILE%"

echo.
echo Setup complete. Log written to:
echo   %LOGFILE%
echo.
pause
endlocal
goto :eof

:: ============================================================
:: LOGGING SUBROUTINE - writes to both console and log file
:: ============================================================
:log
echo %~1
echo %~1 >> "%LOGFILE%"
goto :eof
