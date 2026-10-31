# Remote connector image: CalDAV (iCloud) backend served over HTTP with OAuth.
# Build:  docker build -t apple-calendar-mcp .
# Run:    docker compose up -d   (reads .env, see .env.example)
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --extra remote --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --extra remote

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    FORWARDED_ALLOW_IPS="*"
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"]

CMD ["apple-calendar-mcp", "serve-http"]
