# JobGuard worker image: scheduled GitHub Action runs `run-worker` + digests.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

WORKDIR /app

# uv for reproducible installs; uv.lock is committed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
# evals/ + data/evals/ so `jobguard eval` works inside the image too.
COPY evals/ evals/
COPY data/evals/ data/evals/
COPY data/*.example.yaml data/
RUN uv sync --frozen --no-dev

# Render / docker run: `jobguard serve --host 0.0.0.0 --port $PORT`.
# The venv binary is on PATH for subsequent commands.
ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8000
CMD ["sh", "-c", "jobguard serve --host 0.0.0.0 --port ${PORT:-8000}"]
