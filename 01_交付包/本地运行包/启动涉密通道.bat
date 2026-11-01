@echo off
REM ============================================================
REM  Fenghuo - Classified Channel (isolated instance) launcher
REM
REM  Starts the classified/confidential upload channel as a
REM  SEPARATE process on 127.0.0.1:8001, so that it no longer
REM  shares a port with the public query instance (8000).
REM
REM  This file is intentionally ASCII-only, for the same reason
REM  as the main launcher: cmd.exe reads .bat in the OEM codepage
REM  and Chinese text in a batch file gets mis-parsed.
REM ============================================================
title Fenghuo - Classified Channel (127.0.0.1:8001)
cd /d "%~dp0"

echo ============================================================
echo   Fenghuo - Classified Channel (isolated instance)
echo   Listen: 127.0.0.1:8001   Route: /api/upload only
echo ============================================================
echo.

REM ---------- 0. Already listening? Nothing to do. ----------
netstat -ano | findstr ":8001 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [INFO] Port 8001 is already listening - classified channel is running.
    echo        Close this window if you do not need to start it again.
    ping -n 6 127.0.0.1 >nul
    exit /b 0
)

REM ---------- 1. Find a usable Python ----------
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [ERROR] No usable Python found.
    echo         Install Python 3.11+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)
echo [1/3] Interpreter: %PY%

REM ---------- 2. Dependencies ----------
%PY% -c "import fastapi, uvicorn, docx, openpyxl, fitz, multipart" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Dependencies missing - installing now ^(needs network, 1-3 min^)...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency installation failed. Run manually:
        echo         cd /d "%~dp0"
        echo         %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
) else (
    echo [2/3] Dependencies OK
)

REM ---------- 3. Start the isolated classified instance ----------
echo [3/3] Starting classified channel on 127.0.0.1:8001 ...
start "classified-channel" /min cmd /c "%PY% classified_server.py"

set /a _tries=0
:waitloop
set /a _tries+=1
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":8001 " | findstr "LISTENING" >nul
if not errorlevel 1 goto ready
if %_tries% lss 30 goto waitloop

echo.
echo [ERROR] Classified channel did not come up within 30 seconds.
echo         Run this manually to see the real error:
echo         cd /d "%~dp0"
echo         %PY% classified_server.py
echo.
pause
exit /b 1

:ready
echo.
echo ============================================================
echo   Classified channel is ready.
echo.
echo   Listen : http://127.0.0.1:8001   (loopback only)
echo   Route  : /api/upload
echo   Guard  : any outbound connection during parsing fails
echo.
echo   The public query instance stays on port 8000 and is
echo   unaffected. The normal workflow still works without
echo   starting this script, because the main instance keeps
echo   its own /api/upload entry by default.
echo.
echo   To hand the classified channel over to this instance only:
echo     1) set CLASSIFIED_STANDALONE=1 before starting server.py
echo     2) set CLASSIFIED_API="http://127.0.0.1:8001" in index.html
echo ============================================================
echo.
ping -n 16 127.0.0.1 >nul
exit /b 0
