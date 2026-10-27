# One Python image for migrate, api, worker and loadgen (they differ by command).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependency layer first: only changes when pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app/ app/
COPY alembic.ini ./
COPY alembic/ alembic/
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --no-create-home runwatch
USER runwatch

EXPOSE 8000
CMD ["uvicorn", "--factory", "runwatch.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
