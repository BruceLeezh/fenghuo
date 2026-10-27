@echo off
REM ============================================================
REM  Stop the Fenghuo services.
REM    - public query instance : port 8000
REM    - classified channel    : port 8001 (only if it was started)
REM  ASCII-only on purpose - see the note in the launcher (qi dong.bat).
REM ============================================================
title Stop Fenghuo Services

echo Stopping Fenghuo services ...
echo.

set _found=0

REM ---------- public query instance (8000) ----------
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo   [8000] Nothing was listening - public instance is not running.
) else (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
        echo   [8000] Killing PID: %%a
        taskkill /F /PID %%a >nul 2>&1
        set _found=1
    )
)

REM ---------- classified channel instance (8001) ----------
netstat -ano | findstr ":8001 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo   [8001] Nothing was listening - classified instance is not running.
) else (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 " ^| findstr "LISTENING"') do (
        echo   [8001] Killing PID: %%a
        taskkill /F /PID %%a >nul 2>&1
        set _found=1
    )
)

echo.
if "%_found%"=="1" (
    echo Done.
) else (
    echo Nothing was running. Nothing to do.
)
REM portable wait (timeout.exe needs a console; ping always works)
ping -n 6 127.0.0.1 >nul
