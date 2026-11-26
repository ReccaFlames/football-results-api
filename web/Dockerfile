# The Whistleprint landing page (football-results-web). Build from the repository root:
#   docker build -f web/Dockerfile .
# Only the landing package and its dependencies are installed (not the API or the CLI).
FROM ghcr.io/astral-sh/uv:0.9.15 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
# The workspace root and its lock file; the root project itself is not installed.
COPY pyproject.toml uv.lock ./
COPY web ./web

RUN uv sync --frozen --no-dev --package football-results-web --no-editable

USER 10001
EXPOSE 8001
# --no-proxy-headers: the app must see the connecting proxy's address to append it to
# X-Forwarded-For; the API, not this app, decides which hops to trust.
CMD ["uvicorn", "football_results_web.main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--no-proxy-headers"]
