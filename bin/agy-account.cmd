@echo off
setlocal

set "POLYPHONY_ACCOUNT_SCRIPT=%~dp0..\scripts\agy_account.py"
if not exist "%POLYPHONY_ACCOUNT_SCRIPT%" set "POLYPHONY_ACCOUNT_SCRIPT=%~dp0polyphony-agy-account.py"

if defined AGY_BRIDGE_PYTHON if exist "%AGY_BRIDGE_PYTHON%" goto run_bridge

where py >nul 2>nul
if not errorlevel 1 goto run_py

where python >nul 2>nul
if not errorlevel 1 goto run_python

echo agy-account: Python 3 was not found. 1>&2
exit /b 2

:run_bridge
"%AGY_BRIDGE_PYTHON%" "%POLYPHONY_ACCOUNT_SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_py
py -3 "%POLYPHONY_ACCOUNT_SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_python
python "%POLYPHONY_ACCOUNT_SCRIPT%" %*
exit /b %ERRORLEVEL%
