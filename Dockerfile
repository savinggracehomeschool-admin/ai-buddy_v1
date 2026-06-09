# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

# ── Stage 2: runtime image ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="SGEG Education Coach"
LABEL org.opencontainers.image.description="Canvas LTI AI coach for SGEG students"
LABEL org.opencontainers.image.vendor="Saving Grace Education Group"

WORKDIR /app

RUN addgroup --system sgeg && adduser --system --ingroup sgeg sgeg

COPY --from=builder /app/.venv /app/.venv

COPY src/ ./src/

RUN mkdir -p /data && chown sgeg:sgeg /data

USER sgeg

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

CMD ["uvicorn", "sgeg_nudge.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
