# Maintenance daemon — periodic housekeeping tasks
# (dedup cleanup, TTL expiry, index optimization, vacuum, etc.)
#
# Same base as ingestion: Python 3.12, shared deps from pyproject.toml.
# No GPU, no spaCy, no GeoNames — just the core libraries + store clients.

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from pyproject.toml (cached unless deps change)
COPY pyproject.toml .
RUN mkdir -p src/legba && touch src/legba/__init__.py && \
    pip install --no-cache-dir .

# Copy source code
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8700

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -sf http://localhost:8700/health || exit 1

CMD ["python", "-m", "legba.maintenance"]
