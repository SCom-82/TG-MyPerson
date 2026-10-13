"""Golden TG responses in STORAGE_MODE=new (ТЗ 2026-10-13 §6.3, PR-3 AC).

The same requests and snapshots as tests/test_golden_tg.py, but the reads come
from the per-account tables. The golden seed is mirrored into them for 'work'
and 'golden-ro' (legacy reads were global; every account sees its own copy
now) with the same internal ids, so the responses must match byte for byte.
"""

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.test_golden_tg import (  # noqa: F401 — golden_db / golden_client are fixtures
    API_KEY,
    CASES,
    GOLDEN_DIR,
    _golden_psql,
    golden_client,
    golden_db,
)

_NEW_SEED_SQL = """
SELECT create_account_partitions(id) FROM accounts;
UPDATE accounts SET external_user_id = 102 WHERE alias = 'work';

INSERT INTO people (id, display_name) OVERRIDING SYSTEM VALUE VALUES (1, 'Alice A'), (2, 'Bob'), (3, 'Bot');
INSERT INTO identities (id, person_id, platform, external_user_id, username, first_name, last_name, phone, is_bot)
OVERRIDING SYSTEM VALUE VALUES
  (1, 1, 'telegram', 101, 'alice', 'Alice', 'A', '+70000000101', false),
  (2, 2, 'telegram', 102, 'bob', 'Bob', NULL, NULL, false),
  (3, 3, 'telegram', 103, NULL, 'Bot', NULL, NULL, true);
"""

_PER_ACCOUNT_SQL = """
INSERT INTO users (account_id, id, external_id, identity_id, username, first_name, last_name, phone,
                   is_bot, is_self, first_seen_at, updated_at) VALUES
  ({a}, 1, 101, 1, 'alice', 'Alice', 'A', '+70000000101', false, false, '2026-01-01 10:00:00+00', '2026-01-02 10:00:00+00'),
  ({a}, 2, 102, 2, 'bob',   'Bob',   NULL, NULL,          false, {bob_self}, '2026-01-01 11:00:00+00', '2026-01-02 11:00:00+00'),
  ({a}, 3, 103, 3, NULL,    'Bot',   NULL, NULL,          true,  false, '2026-01-01 12:00:00+00', '2026-01-02 12:00:00+00');

INSERT INTO chats (account_id, id, external_id, chat_type, title, username, description, members_count,
                   is_monitored, last_message_external_id, last_message_at, created_at, updated_at) VALUES
  ({a}, 1, -1001000000001, 'group',   'Golden Group',   'golden_group', 'desc', 42, true,
   12, '2026-02-01 09:00:00+00', '2026-01-01 00:00:00+00', '2026-02-01 09:00:00+00'),
  ({a}, 2, -1001000000002, 'channel', 'Golden Channel', NULL,           NULL,   1000, false,
   21, '2026-02-02 09:00:00+00', '2026-01-01 00:00:00+00', '2026-02-02 09:00:00+00'),
  ({a}, 3, 101,            'private', 'Alice',          'alice',        NULL,   NULL, true,
   NULL, NULL, '2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00');

INSERT INTO messages (account_id, id, chat_id, external_id, sender_user_id, sender_chat_external_id,
                      reply_to_external_id, forward_from_chat_external_id, forward_from_message_external_id,
                      message_type, text, text_html, sent_at, is_outgoing, is_edited, edited_at, views, created_at) VALUES
  ({a}, 1, 1, 11, 1, NULL, NULL, NULL, NULL, 'text', 'hello world', NULL,
   '2026-02-01 08:00:00+00', false, false, NULL, NULL, '2026-02-01 08:00:01+00'),
  ({a}, 2, 1, 12, 2, NULL, 11,   NULL, NULL, 'photo', 'hello photo', '<b>hello</b> photo',
   '2026-02-01 09:00:00+00', true, true, '2026-02-01 09:05:00+00', NULL, '2026-02-01 09:00:01+00'),
  ({a}, 3, 2, 21, NULL, -1001000000002, NULL, -1001000000001, 11, 'text', 'post', NULL,
   '2026-02-02 09:00:00+00', false, false, NULL, 77, '2026-02-02 09:00:01+00');

INSERT INTO media (account_id, id, message_id, attach_index, file_external_id, file_unique_id, file_type,
                   file_name, file_size, mime_type, created_at) VALUES
  ({a}, 1, 2, 0, 'f-1', 'u-1', 'photo', NULL, 12345, 'image/jpeg', '2026-02-01 09:00:02+00');

INSERT INTO sync_state (account_id, chat_id, oldest_external_id, newest_external_id, is_fully_synced,
                        total_synced, last_backfill_at) VALUES
  ({a}, 1, 11, 12, true, 2, '2026-02-03 00:00:00+00');
"""


@pytest.fixture(scope="module")
def golden_new_db(golden_db):  # noqa: F811
    _golden_psql(_NEW_SEED_SQL)
    for alias, bob_self in (("work", "true"), ("golden-ro", "false")):
        sql = _PER_ACCOUNT_SQL.replace("{bob_self}", bob_self).replace(
            "{a}", f"(SELECT id FROM accounts WHERE alias = '{alias}')"
        )
        _golden_psql(sql)
    return golden_db


@pytest_asyncio.fixture
async def golden_new_client(golden_new_db, golden_client, monkeypatch):  # noqa: F811
    import app.storage as storage
    from app.config import settings

    engine = create_async_engine(golden_new_db, poolclass=NullPool)
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(storage, "data_session", async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=storage.DataSession, expire_on_commit=False
    ))
    try:
        yield golden_client
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,method,path,alias,body", CASES, ids=[c[0] for c in CASES])
async def test_tg_response_matches_golden_in_new_mode(golden_new_client, name, method, path, alias, body):
    headers = dict(API_KEY)
    if alias:
        headers["x-session-alias"] = alias
    resp = await golden_new_client.request(method, path, headers=headers, json=body)
    expected = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    actual = {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "body": resp.content.decode("utf-8"),
    }
    assert actual == expected, f"TG response for {name} differs in STORAGE_MODE=new"
