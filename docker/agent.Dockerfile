FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN mkdir -p src/legba && touch src/legba/__init__.py && \
    pip install --no-cache-dir ".[dev]"

# Copy actual source code (only this layer rebuilds on code changes)
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

# Download spaCy English model for NLP tools
RUN python -m spacy download en_core_web_sm

# Download GeoNames cities15000 gazetteer for location normalization (~2MB)
RUN mkdir -p /data/geo && \
    curl -sL https://download.geonames.org/export/dump/cities15000.zip -o /tmp/cities.zip && \
    python -c "import zipfile; zipfile.ZipFile('/tmp/cities.zip').extract('cities15000.txt', '/data/geo')" && \
    rm /tmp/cities.zip

# Entrypoint seeds /agent on first boot, sets PYTHONPATH to /agent/src
COPY docker/agent-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Volumes mounted at runtime:
#   /workspace  — rw, agent work product
#   /agent      — rw, agent source code (self-modifiable, git-tracked)
#   /seed_goal  — ro, immutable seed goal
#   /shared     — rw, supervisor <-> agent comms
#   /logs       — append-only, log drain

ENTRYPOINT ["/entrypoint.sh"]
