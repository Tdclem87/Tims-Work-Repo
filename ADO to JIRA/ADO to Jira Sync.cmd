@echo off
setlocal
pushd "%~dp0"

where powershell.exe >nul 2>&1
if %errorlevel% equ 0 (
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ADO to Jira Sync.ps1"
	exit /b %errorlevel%
)

set "SCRIPT=ado_to_jira_sync.py"
if not exist "%SCRIPT%" set "SCRIPT=clean_ado_to_jira_sync.py"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

:restart
cls
echo.
echo ===== Azure DevOps to Jira Sync =====
echo Starting a new run with %SCRIPT%...
echo Using Python: %PYTHON%
echo.
"%PYTHON%" "%SCRIPT%"
echo.
echo Run completed.
echo The session will restart automatically.
echo Restarting from the first prompt...
goto restart
