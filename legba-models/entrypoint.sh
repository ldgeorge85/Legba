#!/bin/bash
set -e

VENV_DIR="${VENV_DIR:-/data/venv}"
REQUIREMENTS="/app/requirements.txt"

# Create venv on /mnt/data if it doesn't exist (first run only)
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[legba] Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR" --system-site-packages
fi

source "$VENV_DIR/bin/activate"

# Install/upgrade deps if requirements changed
REQ_HASH=$(md5sum "$REQUIREMENTS" | cut -d' ' -f1)
INSTALLED_HASH=""
[ -f "$VENV_DIR/.req_hash" ] && INSTALLED_HASH=$(cat "$VENV_DIR/.req_hash")

if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
    echo "[legba] Installing PyTorch (cu124) ..."
    pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
    echo "[legba] Installing remaining packages ..."
    pip install --no-cache-dir -r "$REQUIREMENTS"
    echo "$REQ_HASH" > "$VENV_DIR/.req_hash"
fi

# Pre-download models on first run (before accepting traffic)
echo "[legba] Ensuring models are cached ..."
python3 /app/download_models.py

echo "[legba] Starting server on :8700 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8700 --log-level info
