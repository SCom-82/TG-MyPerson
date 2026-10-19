"""Migration 013 (plan 2026-10-19 §4 PR-D1, QA R2-1): 012 → 013 → 012 → 013 with the app role present."""

import psycopg2
import pytest

from tests.isolation_env import app_role_url, create_account, ensure_app_role, sql
from tests.test_max_migration import _alembic

ALIAS = "mig013-acc"


def _app_sql(statement: str, params=None) -> None:
    conn = psycopg2.connect(app_role_url().replace("postgresql+asyncpg://", "postgresql://"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
    finally:
        conn.close()


def _revision() -> str:
    return sql("SELECT version_num FROM alembic_version", fetch=True)[0][0]


def _column_default() -> str | None:
    rows = sql("SELECT column_default FROM information_schema.columns WHERE table_name = 'account_write_limits'"
               " AND column_name = 'phone_lookups_per_day'", fetch=True)
    return rows[0][0] if rows else None


@pytest.fixture
def account():
    ensure_app_role()
    sql("DELETE FROM accounts WHERE alias = %s", (ALIAS,))
    account_id = create_account(ALIAS, platform="max", partitions=False)
    try:
        yield account_id
    finally:
        if _revision() != "013":
            _alembic("upgrade", "head")
        sql("DELETE FROM accounts WHERE id = %s", (account_id,))


def test_013_roundtrip(account):
    assert _revision() == "013"
    for _ in range(2):
        _alembic("downgrade", "012")
        assert _column_default() is None
        # a row the code before 013 wrote (no such column yet)
        sql("DELETE FROM account_write_limits WHERE account_id = %s", (account,))
        sql("INSERT INTO account_write_limits (account_id, per_hour) VALUES (%s, 7)", (account,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, 'lookup', 't', now())",
                (account,))

        _alembic("upgrade", "013")
        assert _column_default() == "20"
        assert sql("SELECT per_hour, phone_lookups_per_day FROM account_write_limits WHERE account_id = %s",
                   (account,), fetch=True) == [(7, 20)]
        _app_sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, 'lookup', 'contact_by_phone', now())",
                 (account,))
        _app_sql("UPDATE account_write_limits SET phone_lookups_per_day = 0 WHERE account_id = %s", (account,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            sql("UPDATE account_write_limits SET phone_lookups_per_day = -1 WHERE account_id = %s", (account,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, 'other', 't', now())", (account,))
    # downgrade drops the lookup events (the old CHECK would refuse them)
    _alembic("downgrade", "012")
    assert sql("SELECT count(*) FROM write_events WHERE account_id = %s AND kind = 'lookup'", (account,),
               fetch=True) == [(0,)]
    _alembic("upgrade", "head")
    assert _revision() == "013"
