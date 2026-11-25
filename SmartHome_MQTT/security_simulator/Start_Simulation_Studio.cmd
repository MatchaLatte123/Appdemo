@echo off
setlocal
pushd "%~dp0.."
set "SIM_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%SIM_PY%" (
    echo Run: py -3.13 -m venv security_simulator\.venv
    echo Run: security_simulator\.venv\Scripts\python.exe -m pip install -r security_simulator\requirements.txt
    popd
    pause
    exit /b 1
)
"%SIM_PY%" -X utf8 -B -m security_simulator.studio.main %*
set "SIM_EXIT=%ERRORLEVEL%"
popd
if not "%SIM_EXIT%"=="0" pause
exit /b %SIM_EXIT%
