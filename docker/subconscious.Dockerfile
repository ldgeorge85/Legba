# Subconscious service — background SLM-driven processing
# (pattern detection, salience scoring, memory consolidation, etc.)
#
# Same base as ingestion: Python 3.12, shared deps from pyproject.toml.
# Calls the SLM (Llama 8B) via httpx (already in deps) — no local GPU needed.

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from pyproject.toml (cached unless deps change)
# httpx is already included in project dependencies
COPY pyproject.toml .
RUN mkdir -p src/legba && touch src/legba/__init__.py && \
    pip install --no-cache-dir .

# Copy source code
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8800

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -sf http://localhost:8800/health || exit 1

CMD ["python", "-m", "legba.subconscious"]
