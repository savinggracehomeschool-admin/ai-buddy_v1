FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install uv --no-cache-dir

# Copy dependency files first (better layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ ./src/
COPY scripts/ ./scripts/

# Create data directory for keys / local fallback
RUN mkdir -p /data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "sgeg_nudge.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
