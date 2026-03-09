FROM python:3.12-slim

WORKDIR /app

# System deps (needs docker CLI to manage agent container)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI (to manage agent container from supervisor container)
RUN curl -fsSL https://get.docker.com | sh

# Install Python deps first (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN mkdir -p src/legba && touch src/legba/__init__.py && \
    pip install --no-cache-dir .

# Copy actual source code (only this layer rebuilds on code changes)
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

# Volumes mounted at runtime:
#   /shared     — rw, supervisor <-> agent comms
#   /logs       — rw, log collection
#   /seed_goal  — rw, supervisor owns the seed goal
#   /agent      — rw, agent source code (for rollback on heartbeat failure)
#   /var/run/docker.sock — Docker socket for container management

CMD ["python", "-m", "legba.supervisor.main"]
