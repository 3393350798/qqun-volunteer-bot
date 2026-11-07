@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto VENV
where python >nul 2>&1
if errorlevel 1 goto NO_PYTHON
python -c "import certifi, selenium, ruamel.yaml, tkinter, icalendar, recurring_ical_events" >nul 2>&1
if errorlevel 1 goto INSTALL
python setup_ui.py
goto FINISH
:INSTALL
python -m pip install -r requirements.txt
if errorlevel 1 goto NO_DEPS
python setup_ui.py
goto FINISH
:VENV
".venv\Scripts\python.exe" -c "import certifi, selenium, ruamel.yaml, tkinter, icalendar, recurring_ical_events" >nul 2>&1
if not errorlevel 1 goto RUN_VENV
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto NO_DEPS
:RUN_VENV
".venv\Scripts\python.exe" setup_ui.py
:FINISH
if not errorlevel 1 exit /b 0
echo Application exited with an error.
pause
exit /b 1
:NO_PYTHON
echo Python was not found. Install Python and add it to PATH.
pause
exit /b 1
:NO_DEPS
echo Dependency installation failed.
pause
exit /b 1
