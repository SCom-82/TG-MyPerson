"""app.scripts.max_repair (plan 2026-10-19 Р-10, QA R1-12, R1-13).

Rows as the code before PR-B/PR-C stored them: forwards typed 'text' without
their media, contact cards with an empty text, private chats without a title.
The script repairs them from raw only, under the app role (RLS on).
"""

import io
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.storage as storage
from app.repo import chats, users
from app.repo.rows import ChatRow, UserRow
from app.scripts import max_repair
from app.storage import account_db
from tests.isolation_env import app_role_url, create_account, drop_account, ensure_app_role, sql
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import ME, _authorized, fx
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

DIALOG = 100500
PEER = 200


def _state(account_id: int) -> dict:
    return {
        "messages": sql(f"SELECT external_id, message_type, text FROM messages_a{account_id} ORDER BY external_id",
                        fetch=True),
        "media": sql(f"SELECT m.external_id, d.attach_index, d.file_type FROM media_a{account_id} d"
                     f" JOIN messages_a{account_id} m ON m.id = d.message_id ORDER BY 1, 2", fetch=True),
        "titles": sql(f"SELECT external_id, title FROM chats_a{account_id} ORDER BY external_id", fetch=True),
    }


def _as_old_code(account_id: int) -> None:
    """What b92263c had stored for the same frames."""
    sql(f"UPDATE messages_a{account_id} SET message_type = 'text' WHERE external_id IN (14, 15)")
    sql(f"UPDATE messages_a{account_id} SET message_type = 'contact', text = NULL WHERE external_id IN (16, 72)")
    sql(f"DELETE FROM media_a{account_id} d USING messages_a{account_id} m"
        f" WHERE m.id = d.message_id AND (m.external_id = 14 OR (m.external_id = 15 AND d.attach_index = 1))")
    sql(f"UPDATE chats_a{account_id} SET title = NULL WHERE chat_type = 'private'")


@pytest_asyncio.fixture
async def stored(new_env, monkeypatch):  # noqa: F811
    client = await _authorized(new_env)
    a = new_env.account_id
    for name in ("n14_forward_photo", "n15_own_photo_forward_file", "n16_contact_with_phone", "n07b_contact",
                 "n03_photo"):
        await client.push(fx(name)["frame"])
    async with account_db(a) as db:
        await chats.upsert_chat(db, a, ChatRow(ME ^ PEER, "private", raw={"participants": {str(ME): 1, str(PEER): 1}}))
        await chats.upsert_chat(db, a, ChatRow(0, "private"))
        await users.upsert_user(db, a, "max", UserRow(PEER, first_name="Анна"), "contact")
        await db.commit()
    repaired = _state(a)
    _as_old_code(a)

    # the script runs as the app role in the container: RLS applies
    ensure_app_role()
    engine = create_async_engine(app_role_url(), poolclass=NullPool)
    monkeypatch.setattr(storage, "data_session", async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=storage.DataSession, expire_on_commit=False))
    yield new_env, repaired
    await engine.dispose()


async def test_dry_run_apply_repeat_undo(stored):
    env, repaired = stored
    a = env.account_id
    old = _state(a)
    assert old != repaired

    dry = await max_repair.repair(env.alias, apply=False)
    assert (len(dry["titles"]), len(dry["messages"]), len(dry["media"])) == (2, 4, 2)
    assert _state(a) == old  # nothing written

    journal = await max_repair.repair(env.alias, apply=True)
    assert (len(journal["titles"]), len(journal["messages"]), len(journal["media"])) == (2, 4, 2)
    now = _state(a)
    assert now["messages"] == repaired["messages"] and now["media"] == repaired["media"]
    titles = dict(now["titles"])
    assert titles[ME ^ PEER] == "Анна" and titles[0] == "Избранное" and titles[DIALOG] is None

    again = await max_repair.repair(env.alias, apply=True)
    assert (again["titles"], again["messages"], again["media"]) == ([], [], [])

    # the journal survives JSON (what --apply prints to stdout)
    journal = json.loads(json.dumps(journal, default=str))
    dry_undo = await max_repair.undo(journal, apply=False)
    assert dry_undo["media_removed"] == 2 and _state(a) == now
    done = await max_repair.undo(journal, apply=True)
    assert (done["titles"], done["messages"], done["media_removed"]) == (2, 4, 2)
    assert _state(a) == old


async def test_other_accounts_are_not_touched(stored):
    env, _ = stored
    other_me = 9_400_000_777
    other = create_account("mx-repair-other", platform="max", external_user_id=other_me)
    try:
        sql(f"INSERT INTO chats_a{other} (account_id, external_id, chat_type) VALUES ({other}, 0, 'private')")
        before = _state(other)
        await max_repair.repair(env.alias, apply=True)
        assert _state(other) == before
    finally:
        drop_account(other)


def test_telegram_alias_is_refused(aliases, capsys):  # noqa: F811
    alias, account_id = aliases("tg", platform="telegram", mode="rw")
    assert max_repair.main(["--account", alias, "--apply"]) == 2
    assert "MAX accounts only" in capsys.readouterr().err
    assert max_repair.main(["--account", "no-such-alias"]) == 2


def _cli(argv: list[str]) -> int:
    from app.database import engine

    engine.sync_engine.dispose(close=False)  # pooled connections belong to the test's loop
    return max_repair.main(argv)


def test_cli_dry_run_and_apply_output(stored, capsys, tmp_path, monkeypatch):
    env, _ = stored
    assert _cli(["--account", env.alias]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "dry-run" in out.err and "titles=2, messages=4, media_inserted=2" in out.err

    undo_file = tmp_path / "undo.json"
    assert _cli(["--account", env.alias, "--apply", "--undo-file", str(undo_file)]) == 0
    assert json.loads(undo_file.read_text())["account"] == env.alias
    assert _cli(["--account", env.alias, "--apply"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["media"] == [] and "titles=0, messages=0, media_inserted=0" in out.err
    monkeypatch.setattr("sys.stdin", io.StringIO(undo_file.read_text()))  # --undo - (R13-3)
    assert _cli(["--undo", "-"]) == 0
    assert "dry-run" in capsys.readouterr().err
    assert _cli(["--undo", str(undo_file), "--apply"]) == 0
    assert "media_removed=2" in capsys.readouterr().err
