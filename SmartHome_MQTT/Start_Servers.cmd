@echo off
setlocal
set "launcher_python=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%launcher_python%" set "launcher_python=python.exe"
"%launcher_python%" -X utf8 -B "%~dp0start_servers.py" %*
set "launcher_status=%ERRORLEVEL%"
if not "%launcher_status%"=="0" pause
exit /b %launcher_status%
