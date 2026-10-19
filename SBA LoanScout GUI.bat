@echo off
rem Double-click to open SBA LoanScout. The first time, it sets itself up.
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo First-time setup: preparing SBA LoanScout. This takes a minute...
    python -m venv .venv || goto :nopython
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :failed
)

rem A copy set up before a new package was added gets it now.
".venv\Scripts\python.exe" -c "import dns.resolver" 2>nul || ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :failed

start "" ".venv\Scripts\pythonw.exe" -m sba_loanscout.gui
exit /b 0

:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" on the first screen, then try again.
pause
exit /b 1

:failed
echo.
echo Setup didn't finish - see the messages above.
pause
exit /b 1
