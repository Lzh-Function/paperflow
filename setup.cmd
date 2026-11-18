@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1" %*
set "paperflow_setup_exit=%errorlevel%"
if not "%paperflow_setup_exit%"=="0" (
    echo.
    echo PaperFlow setup failed. See the message above.
    pause
)
exit /b %paperflow_setup_exit%
