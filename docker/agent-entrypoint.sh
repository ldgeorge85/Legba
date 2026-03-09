#!/bin/bash
# Agent entrypoint: ensures /agent has a copy of the source code.
#
# On first boot (empty /agent volume), copies the source from the image.
# On subsequent boots, /agent already has code (possibly self-modified).
# PYTHONPATH is set to /agent/src so the agent loads from the modifiable volume.

set -e

# If /agent/src/legba doesn't exist, seed from the image
if [ ! -d "/agent/src/legba" ]; then
    echo "[entrypoint] First boot: seeding /agent with source from image..."
    cp -r /app/src /agent/src
    cp /app/pyproject.toml /agent/pyproject.toml
    echo "[entrypoint] Source seeded to /agent/src"
else
    echo "[entrypoint] /agent/src exists, loading self-modified code"
fi

# Run the agent with PYTHONPATH pointing to the modifiable volume
export PYTHONPATH="/agent/src:${PYTHONPATH:-}"
exec python -m legba.agent.main
