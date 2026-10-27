# Two stages: install dependencies with uv, then copy only the finished

# virtual environment into a clean image. No compiler, no uv, no source tree.


 

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: rebuilt only when pyproject.toml or

# uv.lock change, not on every code edit. (uv.lock* is optional until the

# first `uv lock`; commit it so builds are reproducible.)

COPY pyproject.toml uv.lock* README.md ./

RUN uv sync --no-dev --no-install-project

COPY src src

RUN uv sync --no-dev --no-editable


 

FROM python:3.12-slim

RUN useradd --system --no-create-home app

WORKDIR /app

COPY --from=build /app/.venv /app/.venv

COPY alembic.ini ./

COPY migrations migrations

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# Never run as root inside the container.

USER app

EXPOSE 8000

CMD ["uvicorn", "ratekeeper.main:app", "--host", "0.0.0.0", "--port", "8000"]


 