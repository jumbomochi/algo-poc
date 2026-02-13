FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for psycopg2-binary and lightgbm
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

# Copy project metadata and install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy shared code and Alembic configuration
COPY shared/ shared/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY config/ config/

# Default command runs Alembic migrations
CMD ["alembic", "upgrade", "head"]
