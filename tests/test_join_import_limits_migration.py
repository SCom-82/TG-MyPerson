"""Migration 015 (plan 2026-10-19 Р4 PR-R4-1, QA R4-1): 014 → 015 → 014 → 015 with the app role present."""

import psycopg2
import pytest

from tests.isolation_env import create_account, ensure_app_role, sql
from tests.test_max_migration import _HEAD, _alembic
from tests.test_phone_lookups_migration import _app_sql, _revision

ALIAS = "mig015-acc"
KINDS = ("chat_join", "contact_import")


def _defaults() -> dict:
    rows = sql("SELECT column_name, column_default FROM information_schema.columns"
               " WHERE table_name = 'account_write_limits'"
               " AND column_name IN ('chats_joined_per_day', 'contacts_imported_per_day')", fetch=True)
    return dict(rows)


@pytest.fixture
def account():
    ensure_app_role()
    sql("DELETE FROM accounts WHERE alias = %s", (ALIAS,))
    account_id = create_account(ALIAS, platform="max", partitions=False)
    try:
        yield account_id
    finally:
        if _revision() != _HEAD:
            _alembic("upgrade", "head")
        sql("DELETE FROM accounts WHERE id = %s", (account_id,))


def test_015_roundtrip(account):
    assert _revision() == _HEAD == "015"
    for _ in range(2):
        _alembic("downgrade", "014")
        assert _defaults() == {}
        sql("DELETE FROM account_write_limits WHERE account_id = %s", (account,))
        sql("INSERT INTO account_write_limits (account_id, per_hour) VALUES (%s, 7)", (account,))  # code of 014
        for kind in KINDS:
            with pytest.raises(psycopg2.errors.CheckViolation):
                sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, %s, 't', now())",
                    (account, kind))

        _alembic("upgrade", "015")
        assert _defaults() == {"chats_joined_per_day": "5", "contacts_imported_per_day": "50"}
        assert sql("SELECT per_hour, group_invites_per_day, chats_joined_per_day, contacts_imported_per_day"
                   " FROM account_write_limits WHERE account_id = %s", (account,), fetch=True) == [(7, 30, 5, 50)]
        for kind in KINDS:
            _app_sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, %s, 'join_chat', now())",
                     (account, kind))
        _app_sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, 'group_invite', 't', now())",
                 (account,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            sql("UPDATE account_write_limits SET chats_joined_per_day = -1 WHERE account_id = %s", (account,))
    _alembic("downgrade", "014")
    assert sql("SELECT kind, count(*) FROM write_events WHERE account_id = %s GROUP BY 1 ORDER BY 1", (account,),
               fetch=True) == [("group_invite", 2)]  # join/import events dropped, invites stay
    _alembic("upgrade", "head")
    assert _revision() == _HEAD
