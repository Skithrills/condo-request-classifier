@echo off
powershell.exe -NoProfile -File "%~dp0scripts\start_project.ps1" %*
if errorlevel 1 (
    echo.
    echo The project could not start. See the message above.
    pause
    exit /b 1
)
