@echo off
setlocal
cd /d "%~dp0"
title tether

if exist ".venv\Scripts\python.exe" goto check
echo First run: setting up tether (about a minute)...
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Could not create a Python environment.
  echo Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

:check
rem Only install when a dependency is actually missing or too old (takes ~0.1s to check).
rem Updating tether's own code never needs a reinstall.
".venv\Scripts\python.exe" tether\_deps.py
if %errorlevel%==0 goto run
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -e ".[dev]"
if errorlevel 1 (
  echo Dependency install failed. Scroll up for the error.
  pause
  exit /b 1
)

:run
".venv\Scripts\python.exe" -m tether app
if errorlevel 1 pause
