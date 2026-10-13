"""T26: POST /accounts creates the account and its partitions in one transaction (ТЗ §2.5)."""

import pytest
from fastapi import HTTPException

import app.api.accounts as accounts_api
from tests.isolation_env import DATA_TABLES, drop_account, sql

ALIAS = "iso-api-new"


@pytest.fixture(autouse=True)
def _cleanup():
    def clean():
        for (acc,) in sql("SELECT id FROM accounts WHERE alias = %s", (ALIAS,), fetch=True):
            drop_account(acc)
    clean()
    yield
    clean()


def _partitions(account_id: int) -> list[str]:
    return [r[0] for r in sql(
        "SELECT partition FROM account_partitions(%s) WHERE rows IS NOT NULL", (account_id,), fetch=True
    )]


async def test_create_account_creates_partitions():
    data = await accounts_api.create_account(accounts_api.AccountCreate(alias=ALIAS, phone="+79000000000"))

    assert _partitions(data["id"]) == [f"{t}_a{data['id']}" for t in DATA_TABLES]


async def test_partition_failure_rolls_back_account(monkeypatch):
    async def broken(db, account_id):
        raise RuntimeError("partition DDL failed")

    monkeypatch.setattr(accounts_api, "create_partitions", broken)

    with pytest.raises(RuntimeError, match="partition DDL failed"):
        await accounts_api.create_account(accounts_api.AccountCreate(alias=ALIAS, phone="+79000000000"))
    assert sql("SELECT count(*) FROM accounts WHERE alias = %s", (ALIAS,), fetch=True) == [(0,)]


async def test_duplicate_alias_still_409():
    await accounts_api.create_account(accounts_api.AccountCreate(alias=ALIAS, phone="+79000000000"))
    with pytest.raises(HTTPException) as exc:
        await accounts_api.create_account(accounts_api.AccountCreate(alias=ALIAS, phone="+79000000001"))
    assert exc.value.status_code == 409
