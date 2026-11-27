@echo off
setlocal
set "PY=%~dp0.studio-venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Studio venv not found. See reports\ciciot2023_rebuild\studio_redesign.md.
  exit /b 1
)
pushd "%~dp0.."
"%PY%" -X utf8 -B -m security_ai.web_ui.studio --open-browser %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
