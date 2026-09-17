@echo off
setlocal

echo ==========================================
echo   Curious About Things Windows Installer
echo ==========================================

echo.
echo Checking Python...

python --version >nul 2>&1

if %errorlevel% neq 0 (
    echo ERROR: Python not found.
    echo Please install Python 3.12 first.
    pause
    exit /b 1
)

python --version

echo.
echo Creating virtual environment...

if not exist ".venv\Scripts\activate.bat" (
    python -m venv .venv

    if %errorlevel% neq 0 (
        echo ERROR: Failed creating virtual environment.
        pause
        exit /b 1
    )
)

echo Virtual environment ready.

echo.
echo Activating virtual environment...

call .venv\Scripts\activate.bat

echo.
echo Upgrading pip...

python -m pip install --upgrade pip

if %errorlevel% neq 0 (
    echo ERROR: Failed upgrading pip.
    pause
    exit /b 1
)

echo.
echo Installing Curious About Things dependencies...

python -m pip install --upgrade -r requirements.txt

if %errorlevel% neq 0 (
    echo ERROR: Failed installing dependencies.
    pause
    exit /b 1
)

echo.
echo Checking FFmpeg...

ffmpeg -version >nul 2>&1

if %errorlevel% neq 0 (

    echo FFmpeg not found.
    echo Installing FFmpeg using winget...

    winget install Gyan.FFmpeg ^
        --accept-package-agreements ^
        --accept-source-agreements

    if %errorlevel% neq 0 (
        echo ERROR: Failed installing FFmpeg.
        pause
        exit /b 1
    )

) else (

    echo FFmpeg detected.

)

echo.
echo Checking Chrome...

reg query "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1

if %errorlevel% neq 0 (

    echo WARNING: Chrome was not detected.
    echo The stock footage provider drives Chrome with Selenium.
    echo Install Google Chrome if footage downloads fail.

) else (

    echo Chrome detected.

)

echo.
echo Checking Ollama...

where ollama >nul 2>&1

if %errorlevel% neq 0 (

    echo Ollama not found.

    echo.
    echo Please install Ollama for Windows from:
    echo https://ollama.com/download/windows

    echo.
    echo After installing Ollama, run this installer again.

    pause
    exit /b 1

) else (

    echo Ollama detected.

)

echo.
echo Checking qwen3:8b model...

ollama list | findstr /C:"qwen3:8b" >nul

if %errorlevel% neq 0 (

    echo qwen3:8b not found.
    echo Downloading qwen3:8b...

    ollama pull qwen3:8b

    if %errorlevel% neq 0 (
        echo ERROR: Failed to pull qwen3:8b.
        pause
        exit /b 1
    )

) else (

    echo qwen3:8b already installed.

)

echo.
echo ==========================================
echo       Installation Complete
echo ==========================================

echo.
echo Run Curious About Things with:
echo.
echo run_windows.bat
echo.

pause