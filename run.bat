@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Newbies Flasher

if not exist "%~dp0main.py" (
    echo Extract the entire archive before running run.bat.
    pause
    exit /b 1
)

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    for /f "tokens=*" %%i in ('where python 2^>nul') do (
        echo "%%i" | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            "%%i" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
            if not errorlevel 1 (
                set "PY_CMD="%%i""
                goto :run_app
            )
        )
    )
)

if not defined PY_CMD (
    echo Python 3.8 or newer was not found on this PC.
    where winget >nul 2>nul
    if not errorlevel 1 (
        set /p INSTALL_PY="Install Python 3.12 via winget now? (y/n): "
        if /i "!INSTALL_PY!"=="y" (
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            echo Close this window and run run.bat again once installation completes.
            pause
            exit /b 0
        )
    )
    echo Download Python 3.8+ from https://www.python.org/downloads/ and check "Add python.exe to PATH".
    pause
    exit /b 1
)

:run_app
if "%~1"=="" (
    %PY_CMD% "%~dp0gui.py"
) else (
    %PY_CMD% "%~dp0main.py" %*
)
if errorlevel 1 pause
exit /b %errorlevel%
