# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.11 AS uv

FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    BOT_DATA_DIR=/app/data \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-cache

COPY bot.py discord_app.py ./
RUN mkdir -p /app/data /app/photos

CMD ["python", "bot.py", "run"]
