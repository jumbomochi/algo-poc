FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for psycopg2-binary and lightgbm
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

# Copy project metadata and install Python dependencies
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --no-deps -r requirements.lock && \
    pip install --no-cache-dir --no-deps .

# Copy shared code and service code
COPY shared/ shared/
COPY services/__init__.py services/__init__.py
COPY services/data_ingestion/ services/data_ingestion/
COPY config/ config/
# The capture universe (KAN-58). shared/universe.py resolves this snapshot from
# a path pinned to the package root — /app in this image — and data_ingestion
# resolves its capture universe at startup. Without this COPY the lookup fails
# and, with restart: unless-stopped, the container crash-loops. Not covered by
# CI, which never builds images: see tests/deploy/test_capture_deployment.py.
COPY data/universe/ data/universe/

ENTRYPOINT ["python", "-m", "services.data_ingestion.runner"]
