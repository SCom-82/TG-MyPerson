# Patch-level tag: python:3.12-slim moves between builds.
FROM python:3.12.15-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies strictly from the lock: exact versions + hashes, nothing resolved
# at build time. A package missing from requirements.lock fails the build.
# How to update — README «Зависимости».
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/

# The project itself only: --no-deps keeps pip from resolving anything new.
RUN pip install --no-cache-dir --no-deps . && pip check

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["sh", "-c", "python -m app.init_db && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
