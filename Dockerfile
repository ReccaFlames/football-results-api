FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh ./

RUN pip install --no-cache-dir . \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R 10001:10001 /app

USER 10001
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]

