@echo off
setlocal
cd /d "%~dp0"
title Newbies Flasher (CLI)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0main.py" %*
    goto :done
)
python "%~dp0main.py" %*

:done
if errorlevel 1 pause
exit /b %errorlevel%
