FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for psycopg2-binary and lightgbm
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

# Copy project metadata and install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy shared code and service code
COPY shared/ shared/
COPY services/__init__.py services/__init__.py
COPY services/execution/ services/execution/
COPY config/ config/

ENTRYPOINT ["python", "-m", "services.execution.runner"]
