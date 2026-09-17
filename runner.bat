@echo off
setlocal EnableExtensions EnableDelayedExpansion

echo ==========================================
echo   Starting Curious About Things
echo ==========================================
echo.

REM ---------------------------------------------------------------------------
REM Activate the project Python virtual environment.

if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo.
    echo ERROR: Python virtual environment not found.
    echo Expected: %~dp0.venv
    echo Please run install_windows.bat first.
    exit /b 1
)

call "%~dp0.venv\Scripts\activate.bat"

if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate the Python virtual environment.
    exit /b 1
)

echo Python virtual environment activated.
echo.

set "OLLAMA_URL=http://localhost:11434"

echo Checking Ollama...

where ollama >nul 2>&1

if errorlevel 1 (
    echo.
    echo ERROR: Ollama is not installed.
    echo Please run install_windows.bat first.
    exit /b 1
)

echo Ollama detected.
echo.

echo Stopping existing Ollama processes...

taskkill /F /IM ollama.exe >nul 2>&1
taskkill /F /IM ollama_llama_server.exe >nul 2>&1
taskkill /F /IM llama-server.exe >nul 2>&1

timeout /t 3 /nobreak >nul

echo Existing Ollama processes stopped.
echo.

echo Starting Ollama...

set "OLLAMA_NO_CLOUD=1"

start "" /B cmd /c "ollama serve >nul 2>&1"

echo Ollama started.
echo.

echo Waiting for Ollama...

set "OLLAMA_READY=0"

for /L %%i in (1,1,30) do (

    curl -s --max-time 2 "%OLLAMA_URL%/api/tags" >nul 2>&1

    if not errorlevel 1 (
        set "OLLAMA_READY=1"
        goto :ollama_ready
    )

    timeout /t 1 /nobreak >nul
)

:ollama_ready

if "%OLLAMA_READY%"=="0" (
    echo.
    echo ERROR: Ollama failed to start.
    echo.
    exit /b 1
)

echo Ollama is ready.
echo.

echo Checking Ollama model...

ollama list | findstr /C:"qwen3:8b" >nul 2>&1

if errorlevel 1 (

    echo qwen3:8b not found.
    echo Pulling qwen3:8b...
    echo.

    ollama pull qwen3:8b

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to pull qwen3:8b.
        exit /b 1
    )

) else (

    echo qwen3:8b detected.

)

echo.

REM ---------------------------------------------------------------------------
REM Pexels Chrome
REM
REM Start a dedicated visible Chrome profile with remote debugging enabled.
REM The Selenium-based stock footage provider attaches to this browser
REM instead of launching its own Chrome. A real, visible browser session
REM passes Pexels' Cloudflare checks reliably, and the persistent profile
REM keeps that clearance between runs.

set "PEXELS_DEBUG_HOST=127.0.0.1"
set "PEXELS_DEBUG_PORT=9222"
set "PEXELS_CHROME_PROFILE=%~dp0media\browser_profile\pexels"

echo ==========================================
echo Starting Pexels Chrome
echo ==========================================
echo.

set "CHROME_EXE="

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    goto :chrome_found
)

if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    goto :chrome_found
)

if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%LocalAppData%\Google\Chrome\Application\chrome.exe"
    goto :chrome_found
)

:chrome_found

if "%CHROME_EXE%"=="" (

    echo WARNING: Chrome not found - the footage provider will
    echo try to launch its own browser.
    echo.

    goto :after_chrome
)

start "Pexels Chrome" /MIN "%CHROME_EXE%" ^
    --remote-debugging-port=%PEXELS_DEBUG_PORT% ^
    --remote-allow-origins=* ^
    --user-data-dir="%PEXELS_CHROME_PROFILE%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --window-size=1200,900 ^
    "https://www.pexels.com"

echo Pexels Chrome started on port %PEXELS_DEBUG_PORT%.
echo If a Cloudflare check appears, solve it once in that window.
echo.

timeout /t 3 /nobreak >nul

:after_chrome

REM ---------------------------------------------------------------------------
REM Cloudflare tunnel
REM
REM Instagram needs a publicly reachable URL to pull the video from, so a
REM temporary tunnel exposes the local server. Local-only mode still works
REM for everything except Instagram publishing.

set "SERVER_PORT=8000"

set "TUNNEL_LOG=%TEMP%\curious_tunnel_log.txt"

set "CLOUDFLARED_EXE="

where cloudflared >nul 2>&1
if not errorlevel 1 (
    set "CLOUDFLARED_EXE=cloudflared"
    goto :cloudflared_found
)

if exist "C:\Program Files\cloudflared\cloudflared.exe" (
    set "CLOUDFLARED_EXE=C:\Program Files\cloudflared\cloudflared.exe"
    goto :cloudflared_found
)

if exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
    set "CLOUDFLARED_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
    goto :cloudflared_found
)

if exist "C:\Tools\cloudflared.exe" (
    set "CLOUDFLARED_EXE=C:\Tools\cloudflared.exe"
    goto :cloudflared_found
)

if exist "%USERPROFILE%\cloudflared.exe" (
    set "CLOUDFLARED_EXE=%USERPROFILE%\cloudflared.exe"
    goto :cloudflared_found
)

:cloudflared_found

if "%CLOUDFLARED_EXE%"=="" (

    echo WARNING: cloudflared not found - running local-only.
    echo Instagram publishing needs a public URL; install cloudflared
    echo ^(winget install Cloudflare.cloudflared^) and rerun this script.
    echo.

    goto :start_server
)

echo Starting Cloudflare tunnel...

del "%TUNNEL_LOG%" >nul 2>&1

start "curious-cloudflared" /MIN cmd /c ""%CLOUDFLARED_EXE%" tunnel --url http://localhost:%SERVER_PORT% > "%TUNNEL_LOG%" 2>&1"

echo Waiting for the public tunnel URL...

set "PUBLIC_URL="
set "TUNNEL_LINE_FILE=%TEMP%\curious_tunnel_line.txt"

for /L %%i in (1,1,30) do (

    if "!PUBLIC_URL!"=="" (

        timeout /t 1 /nobreak >nul

        findstr /R /C:"https://[a-zA-Z0-9-]*\.trycloudflare\.com" "%TUNNEL_LOG%" > "%TUNNEL_LINE_FILE%" 2>nul

        set /p CANDIDATE_LINE=<"!TUNNEL_LINE_FILE!"

        if not "!CANDIDATE_LINE!"=="" (

            set "CANDIDATE_LINE=!CANDIDATE_LINE:*https://=https://!"

            for /f "tokens=1 delims= " %%u in ("!CANDIDATE_LINE!") do (
                set "PUBLIC_URL=%%u"
            )

        )

    )

)

del "%TUNNEL_LINE_FILE%" >nul 2>&1

if "%PUBLIC_URL%"=="" (

    echo WARNING: Tunnel did not report a URL yet - continuing anyway.
    echo Check "%TUNNEL_LOG%" if Instagram publishing fails.
    echo.

) else (

    echo ==========================================================
    echo   Public URL:
    echo   %PUBLIC_URL%
    echo ==========================================================
    echo.

)

:start_server

REM Clear the Ollama-specific environment variable before launching.

set "OLLAMA_NO_CLOUD="

python -m web.server

set "EXIT_CODE=%errorlevel%"

REM Stop the tunnel started earlier (harmless if none was started).

taskkill /F /IM cloudflared.exe >nul 2>&1

echo.

if "%EXIT_CODE%"=="0" (

    echo ==========================================
    echo     Curious About Things Complete
    echo ==========================================

) else (

    echo ==========================================
    echo     Curious About Things Failed
    echo ==========================================
    echo.
    echo Exit code: %EXIT_CODE%

)

exit /b %EXIT_CODE%