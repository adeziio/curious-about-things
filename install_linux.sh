#!/bin/bash

set -e

echo "=========================================="
echo "  Curious About Things Linux Installer"
echo "=========================================="

echo
echo "Checking Python..."

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: Python 3 not found."
    exit 1
fi

python3 --version

echo
echo "Upgrading pip..."

python3 -m pip install --upgrade pip

echo
echo "Installing Curious About Things dependencies..."

python3 -m pip install --upgrade -r requirements.txt

echo
echo "Checking FFmpeg..."

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: FFmpeg not found."
    echo "Please install FFmpeg before continuing."
    exit 1
else
    echo "FFmpeg detected."
fi

echo
echo "Checking Chrome..."

if command -v google-chrome >/dev/null 2>&1; then
    echo "Chrome detected."
else
    echo "WARNING: Chrome was not detected."
    echo "The stock footage provider drives Chrome with Selenium."
    echo "Install Google Chrome if footage downloads fail."
fi

echo
echo "Checking Ollama..."

if ! command -v ollama >/dev/null 2>&1; then
    echo "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "Ollama already installed."
fi

echo
echo "Starting Ollama..."

pkill -f "ollama serve" >/dev/null 2>&1 || true

sleep 2

nohup ollama serve > /tmp/curious-ollama.log 2>&1 &

echo
echo "Waiting for Ollama..."

OLLAMA_READY=false

for i in {1..30}; do

    if curl -s --max-time 2 \
        http://localhost:11434/api/tags \
        >/dev/null 2>&1; then

        OLLAMA_READY=true
        break

    fi

    sleep 1

done

if [ "$OLLAMA_READY" != true ]; then

    echo
    echo "ERROR: Ollama failed to start."

    echo
    echo "Ollama log:"
    echo "------------------------------------------"

    cat /tmp/curious-ollama.log 2>/dev/null || true

    echo "------------------------------------------"

    exit 1

fi

echo "Ollama is ready."

echo
echo "Checking qwen3:8b model..."

if ! ollama list | grep -q "qwen3:8b"; then

    echo "qwen3:8b not found."
    echo "Downloading qwen3:8b..."

    ollama pull qwen3:8b

else

    echo "qwen3:8b already installed."

fi

echo
echo "=========================================="
echo "       Installation Complete"
echo "=========================================="

echo
echo "Run Curious About Things with:"
echo
echo "bash ./run_linux.sh"
echo