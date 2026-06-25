#!/bin/bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
set -e

# Mirror legba-models: keep the venv + any heavy model deps on the mounted
# /data volume so an image rebuild never re-installs torch / re-downloads
# weights. The SHIPPED (seam) service only needs fastapi/uvicorn/pydantic.
VENV_DIR="${VENV_DIR:-/data/venv}"
REQUIREMENTS="/app/app/requirements.txt"
PORT="${LEGBA_MEDIA_PORT:-8800}"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[legba-media] Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR" --system-site-packages
fi

source "$VENV_DIR/bin/activate"

REQ_HASH=$(md5sum "$REQUIREMENTS" | cut -d' ' -f1)
INSTALLED_HASH=""
[ -f "$VENV_DIR/.req_hash" ] && INSTALLED_HASH=$(cat "$VENV_DIR/.req_hash")

if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
    # Install torch on the cu124 wheel index FIRST when a model backend is
    # wired (LEGBA_MEDIA_INSTALL_TORCH=1) so a backend can `import torch`.
    # Off by default — the seam service is CPU-only and needs no torch.
    if [ "${LEGBA_MEDIA_INSTALL_TORCH:-0}" = "1" ]; then
        echo "[legba-media] Installing PyTorch (cu124) ..."
        pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
    fi
    echo "[legba-media] Installing service packages ..."
    pip install --no-cache-dir -r "$REQUIREMENTS"
    echo "$REQ_HASH" > "$VENV_DIR/.req_hash"
fi

echo "[legba-media] Starting media service on :${PORT} ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --log-level info --app-dir /app
