"""Migration 014 (plan 2026-10-19 Р3 PR-G1, QA R3-1): 013 → 014 → 013 → 014 with the app role present."""

import psycopg2
import pytest

from tests.isolation_env import create_account, ensure_app_role, sql
from tests.test_max_migration import _HEAD, _alembic
from tests.test_phone_lookups_migration import _app_sql, _revision

ALIAS = "mig014-acc"
GROUP_KINDS = ("group_create", "group_invite")


def _defaults() -> dict:
    rows = sql("SELECT column_name, column_default FROM information_schema.columns"
               " WHERE table_name = 'account_write_limits'"
               " AND column_name IN ('groups_created_per_day', 'group_invites_per_day')", fetch=True)
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


def test_014_roundtrip(account):
    assert _revision() == _HEAD == "014"
    for _ in range(2):
        _alembic("downgrade", "013")
        assert _defaults() == {}
        sql("DELETE FROM account_write_limits WHERE account_id = %s", (account,))
        sql("INSERT INTO account_write_limits (account_id, per_hour) VALUES (%s, 7)", (account,))  # code of 013
        for kind in GROUP_KINDS:
            with pytest.raises(psycopg2.errors.CheckViolation):
                sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, %s, 't', now())",
                    (account, kind))

        _alembic("upgrade", "014")
        assert _defaults() == {"groups_created_per_day": "3", "group_invites_per_day": "30"}
        assert sql("SELECT per_hour, phone_lookups_per_day, groups_created_per_day, group_invites_per_day"
                   " FROM account_write_limits WHERE account_id = %s", (account,), fetch=True) == [(7, 20, 3, 30)]
        for kind in GROUP_KINDS:
            _app_sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, %s, 'create_group', now())",
                     (account, kind))
        _app_sql("INSERT INTO write_events (account_id, kind, tool, at) VALUES (%s, 'lookup', 't', now())",
                 (account,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            sql("UPDATE account_write_limits SET group_invites_per_day = -1 WHERE account_id = %s", (account,))
    _alembic("downgrade", "013")
    assert sql("SELECT kind, count(*) FROM write_events WHERE account_id = %s GROUP BY 1 ORDER BY 1", (account,),
               fetch=True) == [("lookup", 2)]  # group events dropped, lookups stay
    _alembic("upgrade", "head")
    assert _revision() == _HEAD
