# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv
RUN pip install uv --no-cache-dir

# Copy only dependency files first (better layer caching — code changes don't
# invalidate this layer)
COPY pyproject.toml uv.lock ./

# Install all dependencies into an isolated venv
RUN uv sync --frozen --no-dev --no-editable

# ── Stage 2: runtime image ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="SGEG Education Coach"
LABEL org.opencontainers.image.description="Canvas LTI AI coach for SGEG students"
LABEL org.opencontainers.image.vendor="Saving Grace Education Group"

WORKDIR /app

# Non-root user for security
RUN addgroup --system sgeg && adduser --system --ingroup sgeg sgeg

# Copy the installed venv from builder
COPY --from=builder /build/.venv /app/.venv

# Copy application source
COPY src/ ./src/

# Data directory for LTI keys (used when no external secret store)
RUN mkdir -p /data && chown sgeg:sgeg /data

USER sgeg

# Make venv the active Python
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

CMD ["uvicorn", "sgeg_nudge.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
