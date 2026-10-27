@echo off
REM ============================================================
REM  Fenghuo - Outsourcing Risk Early-Warning System  (launcher)
REM
REM  This file is intentionally ASCII-only.
REM  A .bat containing UTF-8 Chinese is mis-parsed by cmd.exe
REM  (cmd reads batch files in the OEM codepage), which produces
REM  garbled "is not recognized as an internal or external command"
REM  errors. Keeping it pure ASCII makes it work on every locale.
REM ============================================================
title Fenghuo - Outsourcing Risk Early-Warning System
cd /d "%~dp0"

echo ============================================================
echo   Fenghuo - Outsourcing Risk Early-Warning System
echo ============================================================
echo.

REM ---------- 0. Port already listening? Just open the browser. ----------
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [INFO] Port 8000 is already listening - service seems to be running.
    start "" http://127.0.0.1:8000/
    echo        Opened http://127.0.0.1:8000/   You may close this window.
    REM portable 6s wait (timeout.exe needs a console; ping always works)
    ping -n 7 127.0.0.1 >nul
    exit /b 0
)

REM ---------- 1. Find a usable Python ----------
REM  Previously this used a bare "python -m uvicorn ...". If the system
REM  Python had no dependencies, the minimized window flashed and closed
REM  and the user only saw ERR_CONNECTION_REFUSED in the browser.
REM  Now we check explicitly and keep the error on screen.
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
    echo         Install Python 3.11+ and tick "Add Python to PATH"
    echo         during installation.
    echo         Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo [1/4] Interpreter: %PY%

REM ---------- 2. Check dependencies (auto-install if missing) ----------
%PY% -c "import fastapi, uvicorn, docx, openpyxl, fitz, multipart" >nul 2>&1
if errorlevel 1 (
    echo [2/4] Dependencies missing - installing now ^(needs network, 1-3 min^)...
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency installation failed. Run this manually:
        echo         cd /d "%~dp0"
        echo         %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo       Dependencies installed.
) else (
    echo [2/4] Dependencies OK
)

REM ---------- 3. Start the backend ----------
echo [3/4] Starting backend...
start "risk-monitor-backend" /min cmd /c "%PY% server.py"

REM Poll the port for up to 30s instead of a fixed sleep.
set /a _tries=0
:waitloop
set /a _tries+=1
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 goto ready
if %_tries% lss 30 goto waitloop

echo.
echo [ERROR] Backend did not come up within 30 seconds.
echo         Run this manually to see the real error:
echo         cd /d "%~dp0"
echo         %PY% server.py
echo.
pause
exit /b 1

:ready
echo [4/4] Service is ready - opening browser...
start "" http://127.0.0.1:8000/

echo.
echo ============================================================
echo   Started.  URL: http://127.0.0.1:8000/
echo.
echo   Always use the URL above (frontend and backend share origin).
echo   Opening index.html directly via file:// will be blocked by
echo   the browser and "query" will appear to do nothing.
echo.
echo   You may close this window; the service keeps running.
echo   To stop it, run the other .bat in this folder.
echo ============================================================
echo.
ping -n 21 127.0.0.1 >nul
exit /b 0
