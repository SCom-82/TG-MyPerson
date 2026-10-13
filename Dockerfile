# Patch-level tag: python:3.12-slim moves between builds.
FROM python:3.12.15-slim

# pg_dump/pg_restore for app.scripts.account_partitions: client 16 from PGDG,
# the prod server is PostgreSQL 16 (Debian's client 17 writes dumps that
# pg_restore cannot apply to 16: "transaction_timeout"). Key checked by sha256.
ARG PG_CLIENT_VERSION=16.15-1.pgdg13+2
ARG PGDG_KEY_SHA256=0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "${PGDG_KEY_SHA256}  /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc" | sha256sum -c - \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends "postgresql-client-16=${PG_CLIENT_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

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
