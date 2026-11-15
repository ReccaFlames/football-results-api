FROM ghcr.io/astral-sh/uv:0.9.15 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh ./

RUN uv sync --frozen --no-dev \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R 10001:10001 /app

USER 10001
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]

