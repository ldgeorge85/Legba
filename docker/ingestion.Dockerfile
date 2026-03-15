FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir . 2>/dev/null || pip install --no-cache-dir -e .

# Copy source code
COPY src/ src/
RUN pip install --no-cache-dir --no-deps -e .

# GeoNames gazetteer for geo resolution
RUN mkdir -p /data/geo && \
    curl -sL http://download.geonames.org/export/dump/cities15000.zip -o /tmp/cities.zip && \
    python -c "import zipfile; zipfile.ZipFile('/tmp/cities.zip').extractall('/data/geo')" && \
    rm /tmp/cities.zip

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8600

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -sf http://localhost:8600/health || exit 1

CMD ["python", "-m", "legba.ingestion"]
