#!/bin/bash

echo "=========================================="
echo "  Starting Curious About Things"
echo "=========================================="
echo ""

OLLAMA_URL="http://localhost:11434"

echo "Checking Ollama..."

if ! command -v ollama >/dev/null 2>&1; then

    echo "ERROR: Ollama is not installed."
    echo "Please run install_linux.sh first."
    exit 1

fi

echo "Ollama detected."
echo ""

echo "Stopping existing Ollama processes..."

pkill -f "ollama serve" >/dev/null 2>&1 || true
pkill -f "ollama runner" >/dev/null 2>&1 || true
pkill -f "llama-server" >/dev/null 2>&1 || true
pkill -f "ollama" >/dev/null 2>&1 || true

sleep 3

echo "Existing Ollama processes stopped."
echo ""

echo "Starting Ollama..."

nohup ollama serve >/dev/null 2>&1 &

OLLAMA_PID=$!

echo "Ollama started with PID: $OLLAMA_PID"
echo ""

echo "Waiting for Ollama..."

OLLAMA_READY=false

for i in {1..30}; do

    if curl -s --max-time 2 \
        "$OLLAMA_URL/api/tags" \
        >/dev/null 2>&1; then

        OLLAMA_READY=true
        break

    fi

    sleep 1

done

if [ "$OLLAMA_READY" = false ]; then

    echo ""
    echo "ERROR: Ollama failed to start."
    echo ""

    exit 1

fi

echo "Ollama is ready."
echo ""

echo "Checking Ollama model..."

if ollama list | grep -q "qwen3:8b"; then

    echo "qwen3:8b detected."

else

    echo "qwen3:8b not found."
    echo "Pulling qwen3:8b..."
    echo ""

    ollama pull qwen3:8b

    if [ $? -ne 0 ]; then

        echo ""
        echo "ERROR: Failed to pull qwen3:8b."
        exit 1

    fi

fi

echo ""

python3 -m web.server

EXIT_CODE=$?

echo ""

if [ $EXIT_CODE -eq 0 ]; then

    echo "=========================================="
    echo "    Curious About Things Complete"
    echo "=========================================="

else

    echo "=========================================="
    echo "    Curious About Things Failed"
    echo "=========================================="
    echo ""
    echo "Exit code: $EXIT_CODE"

fi

exit $EXIT_CODE