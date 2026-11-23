# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.lock

FROM base AS test
COPY requirements-dev.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.lock
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps . \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app \
    && chown -R app:app /app

USER app

FROM base AS runtime
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts/create_student.py ./scripts/create_student.py
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps . \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
