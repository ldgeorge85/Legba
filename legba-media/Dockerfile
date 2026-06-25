# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# legba-media — hosted media-extraction service (Whisper / VLM / OCR).
#
# The SHIPPED service (the declared seam, no model wired) is CPU-only and tiny:
# it serves /health + a loud 503 on each extraction kind. So the default base
# is a slim python image — `docker compose --profile media up -d` works on any
# box, no GPU. When a real model backend is wired (see app/main.load_backends),
# swap this base for the CUDA runtime image legba-models uses
# (`nvidia/cuda:12.1.1-runtime-ubuntu22.04`) + add the GPU `deploy.resources`
# block in docker-compose.yml; the entrypoint already installs torch on first
# run so a backend can import it.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# App code in the image (small, changes rarely).
COPY app/ /app/app/

# Everything heavy (venv, HF model cache) lives on the mounted /data volume so
# rebuilding the image never re-downloads weights — same split as legba-models.
ENV VENV_DIR=/data/venv
ENV HF_HOME=/data/hf-cache
ENV TRANSFORMERS_CACHE=/data/hf-cache

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8800

# Liveness: /health is open + cheap and reports the seam state.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8800/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
