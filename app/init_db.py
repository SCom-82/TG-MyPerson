"""Create the tg_myperson database if it doesn't exist.

Run before alembic migrations. Connects to the default 'postgres' database
to issue CREATE DATABASE, since asyncpg/alembic can't connect to a
non-existent database.
"""

import os
import re
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    match = re.match(r"postgresql\+asyncpg://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", db_url)
    if not match:
        print(f"Cannot parse DATABASE_URL, skipping DB creation: {db_url[:50]}...")
        return

    user, password, host, port, target_db = match.groups()

    conn = psycopg2.connect(dbname="postgres", user=user, password=password, host=host, port=port)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
    if cur.fetchone():
        print(f"Database '{target_db}' already exists.")
    else:
        cur.execute(f'CREATE DATABASE "{target_db}"')
        print(f"Database '{target_db}' created.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
