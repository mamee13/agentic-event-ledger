FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev tools, no editable install)
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ ./src/
COPY main.py ./

# Apply schema on startup via entrypoint script
COPY src/ledger/infrastructure/db/schema.sql ./schema.sql

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uv", "run", "python", "main.py"]
