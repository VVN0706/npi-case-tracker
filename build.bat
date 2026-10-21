@echo off
echo === Build NPI case tracker exe ===
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm case_tracker.spec
echo.
echo Done. Copy the exe in dist\ to where it should run.
echo case_tracker.db is created next to the exe on first launch.
pause
