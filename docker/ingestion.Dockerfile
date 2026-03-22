FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir . 2>/dev/null || pip install --no-cache-dir -e .

# spaCy NER for entity extraction during normalization
RUN pip install --no-cache-dir spacy && python -m spacy download en_core_web_sm

# Telethon for Telegram channel ingestion (optional, activated via TELEGRAM_ENABLED)
RUN pip install --no-cache-dir telethon

# Copy source code
COPY src/ src/
RUN pip install --no-cache-dir --no-deps -e .

# GeoNames gazetteer for geo resolution (cities5000 = 50K+ cities vs 23K in cities15000)
RUN mkdir -p /data/geo && \
    curl -sL http://download.geonames.org/export/dump/cities5000.zip -o /tmp/cities.zip && \
    python -c "import zipfile; zipfile.ZipFile('/tmp/cities.zip').extractall('/data/geo')" && \
    rm /tmp/cities.zip && \
    curl -sL http://download.geonames.org/export/dump/admin1CodesASCII.txt -o /data/geo/admin1CodesASCII.txt

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8600

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -sf http://localhost:8600/health || exit 1

CMD ["python", "-m", "legba.ingestion"]
