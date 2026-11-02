@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   AI Presentation Narrator - Safe Distribution Packager
echo ==========================================================

set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
)

"%PYTHON%" package.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] Packaging stopped due to security check failure or error.
    pause
    exit /b 1
)

echo.
pause
