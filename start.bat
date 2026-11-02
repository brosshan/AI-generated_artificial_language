@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "APP_URL=http://127.0.0.1:8000"
if not defined OLLAMA_MODEL (
    where.exe ollama >nul 2>nul
    if not errorlevel 1 (
        ollama list 2>nul | findstr /i /c:"gemma4:31b-cloud" >nul
        if not errorlevel 1 (
            set "OLLAMA_MODEL=gemma4:31b-cloud"
        )
    )
)
if not defined OLLAMA_MODEL set "OLLAMA_MODEL=qwen2.5-vl"

if not exist "%PYTHON%" (
    echo ERROR: Project virtual environment was not found:
    echo   %PYTHON%
    echo Create it and install requirements first. See README.md.
    pause
    exit /b 1
)

curl.exe --silent --fail "%APP_URL%/" >nul 2>nul
if not errorlevel 1 (
    echo The web app is already running.
    start "" "%APP_URL%"
    exit /b 0
)

"%PYTHON%" -c "import fastapi, uvicorn, multipart, pymupdf, httpx, numpy" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Required web application packages are missing from .venv.
    echo Install them with:
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

curl.exe --silent --fail http://127.0.0.1:11434/api/tags >nul 2>nul
if errorlevel 1 (
    where.exe ollama >nul 2>nul
    if errorlevel 1 (
        echo WARNING: Ollama was not found. Script generation will not work until it is installed.
        goto start_app
    )
    echo Starting Ollama...
    start "Ollama" /min ollama serve
    for /l %%I in (1,1,20) do (
        timeout /t 2 /nobreak >nul
        curl.exe --silent --fail http://127.0.0.1:11434/api/tags >nul 2>nul
        if not errorlevel 1 goto ollama_ready
    )
    echo WARNING: Ollama did not respond. Check its service before generating scripts.
    goto start_app
)

:ollama_ready
where.exe ollama >nul 2>nul
if not errorlevel 1 (
    ollama list 2>nul | findstr /i /c:"%OLLAMA_MODEL%" >nul
    if errorlevel 1 (
        echo WARNING: Ollama model "%OLLAMA_MODEL%" is not installed.
        echo Install it with: ollama pull %OLLAMA_MODEL%
    )
)

:start_app
where.exe ffmpeg >nul 2>nul
if errorlevel 1 echo WARNING: FFmpeg was not found. Video composition will not work.

if not defined SOFFICE_PATH (
    if exist "%ProgramFiles%\LibreOffice\program\soffice.exe" (
        set "SOFFICE_PATH=%ProgramFiles%\LibreOffice\program\soffice.exe"
    ) else if exist "%ProgramFiles(x86)%\LibreOffice\program\soffice.exe" (
        set "SOFFICE_PATH=%ProgramFiles(x86)%\LibreOffice\program\soffice.exe"
    )
)

set "HAS_SOFFICE="
if defined SOFFICE_PATH (
    if exist "%SOFFICE_PATH%" set "HAS_SOFFICE=1"
)
if not defined HAS_SOFFICE (
    where.exe soffice >nul 2>nul
    if not errorlevel 1 set "HAS_SOFFICE=1"
)
if not defined HAS_SOFFICE (
    echo NOTE: LibreOffice was not found. PPT/PPTX conversion requires LibreOffice; PDF is supported directly.
)

echo.
echo Opening %APP_URL%
echo Keep this window open while using the app. Press Ctrl+C to stop it.
start "" "%APP_URL%"
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo The web app has stopped.
pause
