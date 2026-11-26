FROM ghcr.io/astral-sh/uv:0.9.15 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    FOOTBALL_RESULTS_CONFIG_FILE=/app/config/competitions.yaml

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md LICENSE alembic.ini ./
COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY skills ./skills
COPY docker-entrypoint.sh ./

RUN uv sync --frozen --no-dev \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R 10001:10001 /app

USER 10001
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]

