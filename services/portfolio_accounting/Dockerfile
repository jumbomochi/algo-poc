FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --no-deps -r requirements.lock && \
    pip install --no-cache-dir --no-deps .

COPY shared/ shared/
COPY scripts/ scripts/
# projector -> scripts.paper_state -> backtest.portfolio_context
COPY backtest/ backtest/
COPY services/__init__.py services/__init__.py
COPY services/portfolio_accounting/ services/portfolio_accounting/
COPY config/ config/

ENTRYPOINT ["python", "-m", "services.portfolio_accounting.runner"]
