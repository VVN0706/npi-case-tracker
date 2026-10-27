@echo off
setlocal

rem Bare "python" can point at an old install, so prefer the project venv, then the py launcher.
set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY set "PY=python"

echo === Build NPI case tracker exe ===
echo Using:
%PY% --version
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.8 or newer is required. Install a newer Python and try again.
    pause
    exit /b 1
)

%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo ERROR: pip install failed - see the messages above.
    pause
    exit /b 1
)

%PY% -m PyInstaller --noconfirm case_tracker.spec
if errorlevel 1 (
    echo ERROR: build failed - see the messages above. If the exe in dist is still running, close it first.
    pause
    exit /b 1
)

echo.
echo Done. Copy the exe in dist\ to where it should run.
echo case_tracker.db is created next to the exe on first launch.
pause
