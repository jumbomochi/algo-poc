FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY shared/ shared/
COPY scripts/ scripts/
COPY services/__init__.py services/__init__.py
COPY services/portfolio_accounting/ services/portfolio_accounting/
COPY config/ config/

ENTRYPOINT ["python", "-m", "services.portfolio_accounting.runner"]
