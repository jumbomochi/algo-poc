FROM python:3.14-slim

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
COPY services/api/ services/api/
COPY config/ config/

EXPOSE 8000

ENTRYPOINT ["python", "-m", "services.api.runner"]
