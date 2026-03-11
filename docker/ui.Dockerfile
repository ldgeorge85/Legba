FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Download GeoNames cities15000 gazetteer for map geo-resolution (~2MB)
RUN mkdir -p /data/geo && \
    curl -sL https://download.geonames.org/export/dump/cities15000.zip -o /tmp/cities.zip && \
    python -c "import zipfile; zipfile.ZipFile('/tmp/cities.zip').extract('cities15000.txt', '/data/geo')" && \
    rm /tmp/cities.zip

# Install Python deps first (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN mkdir -p src/legba && touch src/legba/__init__.py && \
    pip install --no-cache-dir .

# Copy actual source code (only this layer rebuilds on code changes)
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8501
CMD ["python", "-m", "uvicorn", "legba.ui.app:app", "--host", "0.0.0.0", "--port", "8501"]
