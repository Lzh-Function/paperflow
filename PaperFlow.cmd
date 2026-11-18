@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv-win\Scripts\pythonw.exe" (
    call "%~dp0setup.cmd"
    exit /b
)
if not exist "%~dp0config.production.yaml" (
    call "%~dp0setup.cmd"
    exit /b
)
start "" "%~dp0.venv-win\Scripts\pythonw.exe" -X utf8 -m paperflow.gui --config "%~dp0config.production.yaml"
