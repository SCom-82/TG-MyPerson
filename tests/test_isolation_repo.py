"""Account isolation on the repo level, under the non-superuser app role.

ТЗ 2026-10-13 §12: T1–T8, T23–T25, T27, T29, T30 (T26 → test_accounts_partitions_api,
T28 → test_account_partitions_script).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.models import Identity, Message, PeopleMergeLog, Person, User
from app.repo import chats, media, messages, people, raw_events, snapshots, sync_state, users
from app.repo.rows import ChatRow, MediaRow, MessageRow, SnapshotMemberRow, UserRow
from app.storage import account_db, data_session, owner_db
from tests.isolation_env import (
    DATA_TABLES,
    SELF_A,
    app_scalar,
    create_account,
    drop_account,
    has_privilege,
    sql,
)

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CHAT_EXT = -1_009_400_000_100
PEER = 9_400_000_200
PEER2 = 9_400_000_201


async def _seed(account_id: int, text_: str, *, sender: int = PEER, msg: int = 500) -> dict:
    """Chat, sender, message with media, sync state, snapshot, raw event for one account."""
    async with account_db(account_id) as db:
        chat = await chats.upsert_chat(db, account_id, ChatRow(CHAT_EXT, "supergroup", title="Shared"))
        user = await users.upsert_user(
            db, account_id, "telegram", UserRow(sender, username="peer", first_name="Peer"), "message"
        )
        message = await messages.upsert_message(db, account_id, chat, MessageRow(
            external_id=msg, sent_at=T0, sender_external_id=sender, text=text_,
            media=[MediaRow("photo", file_external_id=f"f-{account_id}")],
        ))
        await sync_state.upsert_state(db, account_id, chat, newest_external_id=msg, total_synced=1)
        snap = await snapshots.create_snapshot(
            db, account_id, chat, [SnapshotMemberRow(external_user_id=sender, username="peer")], members_count=1
        )
        event = await raw_events.add_raw_event(db, account_id, "tg:test", {"n": account_id})
        await db.commit()
    return {"chat": chat, "user": user, "message": message, "snapshot": snap, "event": event}


# ---------------------------------------------------------------------------
# T1, T2
# ---------------------------------------------------------------------------

async def test_t1_same_external_ids_two_accounts_two_rows(accounts):
    await _seed(accounts.a, "text of A")
    await _seed(accounts.b, "text of B")

    async with owner_db() as db:
        rows = (await db.execute(
            select(Message.account_id, Message.text)
            .where(Message.account_id.in_((accounts.a, accounts.b)), Message.external_id == 500)
            .order_by(Message.account_id)
        )).all()
    assert [tuple(r) for r in rows] == [(accounts.a, "text of A"), (accounts.b, "text of B")]


async def test_t2_account_session_sees_only_own_rows(accounts):
    await _seed(accounts.a, "A")
    await _seed(accounts.b, "B")

    for me, other in ((accounts.a, accounts.b), (accounts.b, accounts.a)):
        async with account_db(me) as db:
            for table in DATA_TABLES:
                seen = set((await db.execute(text(f"SELECT DISTINCT account_id FROM {table}"))).scalars())
                assert seen == {me}, f"{table}: account {me} sees {seen}"
            # Even an explicit filter on the other account returns nothing.
            assert (await db.execute(
                text("SELECT count(*) FROM messages WHERE account_id = :o"), {"o": other}
            )).scalar() == 0


# ---------------------------------------------------------------------------
# T3: fail-closed sessions
# ---------------------------------------------------------------------------

async def test_t3_session_without_account_fails_closed(accounts):
    async with data_session() as db:
        with pytest.raises(RuntimeError, match="without account_id"):
            await db.execute(text("SELECT 1"))

    with pytest.raises(RuntimeError):
        async with account_db("1"):  # type: ignore[arg-type]
            pass

    async with account_db(accounts.a) as db:
        with pytest.raises(RuntimeError, match="does not match"):
            await chats.chat_pk(db, accounts.b, CHAT_EXT)


async def test_t3_scope_is_reapplied_after_commit(accounts):
    await _seed(accounts.a, "A")
    await _seed(accounts.b, "B")
    async with account_db(accounts.a) as db:
        await db.commit()
        await db.commit()
        assert set((await db.execute(text("SELECT account_id FROM messages"))).scalars()) == {accounts.a}


# ---------------------------------------------------------------------------
# T4, T5, T6, T7
# ---------------------------------------------------------------------------

async def test_t4_app_role_without_set_config_sees_nothing(accounts):
    await _seed(accounts.a, "A")
    for table in DATA_TABLES:
        assert await app_scalar(accounts.app_engine, f"SELECT count(*) FROM {table}") == 0, table


async def test_t5_with_check_rejects_foreign_account_id(accounts):
    await _seed(accounts.b, "B")
    async with account_db(accounts.a) as db:
        with pytest.raises(ProgrammingError, match="row-level security"):
            await db.execute(
                text("INSERT INTO raw_events (account_id, kind) VALUES (:b, 'x')"), {"b": accounts.b}
            )


async def test_t6_composite_fk_rejects_cross_account_message(accounts):
    seeded_b = await _seed(accounts.b, "B")
    async with owner_db() as db:
        with pytest.raises(IntegrityError, match="foreign key"):
            await db.execute(
                text("INSERT INTO messages (account_id, chat_id, external_id, sent_at)"
                     " VALUES (:a, :chat_of_b, 1, now())"),
                {"a": accounts.a, "chat_of_b": seeded_b["chat"]},
            )


async def test_t7_one_self_per_account(accounts):
    async with account_db(accounts.a) as db:
        await users.upsert_user(db, accounts.a, "telegram", UserRow(SELF_A, first_name="Me"), "dialog")
        await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER, first_name="Peer"), "dialog")
        await db.commit()
        flags = dict((await db.execute(select(User.external_id, User.is_self))).all())
        assert flags == {SELF_A: True, PEER: False}
        with pytest.raises(IntegrityError, match="duplicate key"):  # uq_users_self (per-partition name)
            await db.execute(text("UPDATE users SET is_self = true WHERE external_id = :p"), {"p": PEER})


# ---------------------------------------------------------------------------
# T8: identities / people
# ---------------------------------------------------------------------------

async def _identities(external_ids):
    async with owner_db() as db:
        return (await db.execute(
            select(Identity.platform, Identity.external_user_id, Identity.person_id, Identity.person_pinned)
            .where(Identity.external_user_id.in_(external_ids))
            .order_by(Identity.platform, Identity.external_user_id)
        )).all()


async def test_t8_one_identity_two_users(accounts):
    for acc in (accounts.a, accounts.b):
        async with account_db(acc) as db:
            await users.upsert_user(db, acc, "telegram", UserRow(PEER, username="peer"), "message")
            await db.commit()

    idents = await _identities([PEER])
    assert len(idents) == 1
    async with owner_db() as db:
        rows = (await db.execute(
            select(User.account_id, User.identity_id).where(User.external_id == PEER)
            .order_by(User.account_id)
        )).all()
    assert [r.account_id for r in rows] == [accounts.a, accounts.b]
    assert {r.identity_id for r in rows} == {
        (await _identity_id("telegram", PEER))
    }


async def _identity_id(platform, ext):
    async with owner_db() as db:
        return (await db.execute(
            select(Identity.id).where(Identity.platform == platform, Identity.external_user_id == ext)
        )).scalar_one()


async def test_t8_identity_fields_update_only_when_non_empty(accounts):
    async with account_db(accounts.a) as db:
        await users.upsert_user(db, accounts.a, "telegram",
                                UserRow(PEER, username="peer", first_name="Ivan", phone="79001234567"), "message")
        await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER, username=None, first_name="Иван"),
                                "message")
        await db.commit()
    async with owner_db() as db:
        ident = (await db.execute(select(Identity).where(Identity.external_user_id == PEER))).scalar_one()
    assert (ident.username, ident.first_name, ident.phone) == ("peer", "Иван", "+79001234567")


async def test_t8_auto_merge_tg_max_by_phone(accounts):
    async with account_db(accounts.a) as db:
        await users.upsert_user(db, accounts.a, "telegram",
                                UserRow(PEER, first_name="Ivan", phone="79001234567"), "contact")
        await db.commit()
    async with owner_db() as db:
        tg_person = (await db.execute(
            select(Identity.person_id).where(Identity.external_user_id == PEER)
        )).scalar_one()
        await db.execute(text("UPDATE people SET tags = '{vip}', notes = 'from tg' WHERE id = :p"), {"p": tg_person})
        await db.commit()

    async with account_db(accounts.m) as db:
        await users.upsert_user(db, accounts.m, "max",
                                UserRow(PEER2, first_name="Иван", phone="+7 900 123-45-67"), "message")
        await db.commit()

    idents = await _identities([PEER, PEER2])
    assert {i.person_id for i in idents} == {tg_person}  # MAX identity moved to the older person
    async with owner_db() as db:
        person = await db.get(Person, tg_person)
        log = (await db.execute(
            select(PeopleMergeLog).where(PeopleMergeLog.to_person_id == tg_person)
        )).scalars().all()
    assert person.tags == ["vip"]
    assert [entry.action for entry in log] == ["auto_merge"]


async def test_t8_pinned_identity_blocks_auto_merge(accounts):
    async with account_db(accounts.a) as db:
        await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER, phone="79001234567"), "contact")
        await db.commit()
    sql("UPDATE identities SET person_pinned = true WHERE external_user_id = %s", (PEER,))

    async with account_db(accounts.m) as db:
        await users.upsert_user(db, accounts.m, "max", UserRow(PEER2, phone="89001234567"), "message")
        await db.commit()

    idents = await _identities([PEER, PEER2])
    assert len({i.person_id for i in idents}) == 2


async def test_t8_manual_merge_and_split_are_logged(accounts):
    async with account_db(accounts.a) as db:
        await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER, first_name="A"), "message")
        await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER2, first_name="B"), "message")
        await db.commit()
    p1, p2 = [i.person_id for i in await _identities([PEER, PEER2])]

    async with account_db(accounts.a) as db:
        with pytest.raises(RuntimeError, match="owner"):
            await people.merge_people(db, p2, p1)

    async with owner_db() as db:
        await people.merge_people(db, p2, p1, reason="same human")
        await db.commit()
    idents = await _identities([PEER, PEER2])
    assert {i.person_id for i in idents} == {p1}
    assert all(i.person_pinned for i in idents)
    async with owner_db() as db:
        assert await db.get(Person, p2) is None

        new_person = await people.split_identity(db, await _identity_id("telegram", PEER2))
        await db.commit()
        actions = (await db.execute(
            select(PeopleMergeLog.action).where(PeopleMergeLog.to_person_id.in_((p1, new_person)))
            .order_by(PeopleMergeLog.id)
        )).scalars().all()
    assert actions == ["merge", "split"]
    assert {i.person_id for i in await _identities([PEER, PEER2])} == {p1, new_person}


# ---------------------------------------------------------------------------
# T23–T25: partitions
# ---------------------------------------------------------------------------

async def test_t23_no_partition_no_insert(accounts):
    bare = create_account("iso-bare", partitions=False)
    try:
        async with account_db(bare) as db:
            with pytest.raises(DBAPIError, match="no partition"):
                await raw_events.add_raw_event(db, bare, "tg:test", None)
    finally:
        drop_account(bare)


async def test_t24_create_account_partitions(accounts):
    parts = [r[0] for r in sql("SELECT create_account_partitions(%s)", (accounts.a,), fetch=True)]
    assert parts == [f"{t}_a{accounts.a}" for t in DATA_TABLES]  # idempotent rerun
    for part in parts:
        [(rls,)] = sql("SELECT relrowsecurity FROM pg_class WHERE relname = %s", (part,), fetch=True)
        [(policies,)] = sql(
            "SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname = 'acc_isolation'",
            (part,), fetch=True,
        )
        assert rls and policies == 1, part
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not has_privilege(part, priv), f"{priv} on {part}"
    for table in DATA_TABLES:
        assert has_privilege(table, "SELECT") and has_privilege(table, "INSERT"), table


async def test_t25_direct_partition_access_denied(accounts):
    await _seed(accounts.a, "A")
    with pytest.raises(ProgrammingError, match="permission denied"):
        await app_scalar(accounts.app_engine, f"SELECT count(*) FROM messages_a{accounts.a}")
    async with account_db(accounts.a) as db:
        assert (await db.execute(text("SELECT count(*) FROM messages"))).scalar() == 1


# ---------------------------------------------------------------------------
# T27: partition pruning
# ---------------------------------------------------------------------------

async def test_t27_messages_query_scans_one_partition(accounts):
    await _seed(accounts.a, "A")
    await _seed(accounts.b, "B")
    async with account_db(accounts.a) as db:
        await db.execute(text("SET LOCAL enable_seqscan = on"))
        plan = "\n".join((await db.execute(text(
            "EXPLAIN SELECT * FROM messages WHERE account_id = :a"
            " AND chat_id IN (SELECT id FROM chats WHERE account_id = :a AND external_id = :c)"
            " ORDER BY sent_at DESC LIMIT 50"
        ), {"a": accounts.a, "c": CHAT_EXT})).scalars())
    assert f"messages_a{accounts.a}" in plan
    assert f"messages_a{accounts.b}" not in plan
    assert f"chats_a{accounts.b}" not in plan


# ---------------------------------------------------------------------------
# T29: ON CONFLICT on partitioned tables
# ---------------------------------------------------------------------------

async def test_t29_upserts_do_not_duplicate(accounts):
    first = await _seed(accounts.a, "v1")
    async with account_db(accounts.a) as db:
        chat = await chats.upsert_chat(db, accounts.a, ChatRow(CHAT_EXT, "supergroup", title="Renamed"))
        user = await users.upsert_user(db, accounts.a, "telegram", UserRow(PEER, first_name="Peer2"), "member")
        msg = await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=500, sent_at=T0, sender_external_id=PEER, text="v2",
            media=[MediaRow("photo", file_external_id="f2")],
        ))
        await sync_state.upsert_state(db, accounts.a, chat, newest_external_id=600)
        await db.commit()
        assert (chat, user, msg) == (first["chat"], first["user"], first["message"])
        counts = {
            t: (await db.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
            for t in ("chats", "users", "messages", "media", "sync_state")
        }
        state = await sync_state.get_state(db, accounts.a, chat)
        [m] = await media.list_media(db, accounts.a, msg)
    assert counts == {"chats": 1, "users": 1, "messages": 1, "media": 1, "sync_state": 1}
    assert (state.newest_external_id, state.total_synced) == (600, 1)
    assert m.file_external_id == "f2"


# ---------------------------------------------------------------------------
# Message merge rules (§4.2)
# ---------------------------------------------------------------------------

async def test_message_rules_edit_deleted_views_outgoing(accounts):
    async with account_db(accounts.a) as db:
        chat = await chats.upsert_chat(db, accounts.a, ChatRow(CHAT_EXT, "supergroup"))
        await users.upsert_user(db, accounts.a, "telegram", UserRow(SELF_A), "dialog")
        await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=T0, sender_external_id=SELF_A, text="draft", views=5,
            is_outgoing_hint=False,  # wrong hint: the account's own id decides
        ))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=T0, sender_external_id=SELF_A, text="final",
            is_edited=True, edited_at=T0 + timedelta(minutes=1),
        ))
        await messages.mark_deleted(db, accounts.a, chat, [1], T0 + timedelta(hours=1))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=T0, sender_external_id=SELF_A, text="draft",
        ))
        await db.commit()
        [msg], total = await messages.get_messages(db, accounts.a, chat_external_id=CHAT_EXT)
    assert total == 1
    assert (msg.text, msg.is_edited, msg.views, msg.is_outgoing) == ("final", True, 5, True)
    assert msg.deleted_at == T0 + timedelta(hours=1)


async def test_mark_deleted_without_chat_touches_private_chats_of_own_account(accounts):
    """T20 precondition on the repo level: update without chat id."""
    for acc in (accounts.a, accounts.b):
        async with account_db(acc) as db:
            private = await chats.upsert_chat(db, acc, ChatRow(PEER, "private"))
            channel = await chats.upsert_chat(db, acc, ChatRow(CHAT_EXT, "channel"))
            for chat in (private, channel):
                await messages.upsert_message(db, acc, chat, MessageRow(external_id=77, sent_at=T0))
            await db.commit()

    async with account_db(accounts.a) as db:
        assert await messages.mark_deleted(db, accounts.a, None, [77], T0) == 1
        await db.commit()

    async with owner_db() as db:
        deleted = (await db.execute(text(
            "SELECT m.account_id, c.chat_type FROM messages m"
            " JOIN chats c ON c.account_id = m.account_id AND c.id = m.chat_id"
            " WHERE m.external_id = 77 AND m.deleted_at IS NOT NULL"
            " AND m.account_id IN (:a, :b)"
        ), {"a": accounts.a, "b": accounts.b})).all()
    assert [tuple(r) for r in deleted] == [(accounts.a, "private")]


async def test_last_message_never_moves_back(accounts):
    async with account_db(accounts.a) as db:
        chat = await chats.upsert_chat(db, accounts.a, ChatRow(CHAT_EXT, "supergroup"))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(external_id=10, sent_at=T0))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(external_id=5, sent_at=T0 - timedelta(days=1)))
        await chats.upsert_chat(db, accounts.a, ChatRow(
            CHAT_EXT, "supergroup", last_message_external_id=3, last_message_at=T0 - timedelta(days=2)
        ))
        await db.commit()
        row = await chats.get_chat(db, accounts.a, CHAT_EXT)
    assert (row.last_message_external_id, row.last_message_at) == (10, T0)


async def test_access_hash_min_entity_does_not_overwrite(accounts):
    """T13 on the repo level: a None hash (min entity) keeps the stored one."""
    async with account_db(accounts.b) as db:
        await users.upsert_user(db, accounts.b, "telegram", UserRow(PEER, access_hash=111), "message")
        await users.upsert_user(db, accounts.b, "telegram", UserRow(PEER, access_hash=None), "message")
        await chats.upsert_chat(db, accounts.b, ChatRow(CHAT_EXT, "channel", access_hash=222))
        await chats.upsert_chat(db, accounts.b, ChatRow(CHAT_EXT, "channel", access_hash=None))
        await db.commit()
        user = await users.get_user(db, accounts.b, PEER)
        chat = await chats.get_chat(db, accounts.b, CHAT_EXT)
    assert (user.access_hash, chat.access_hash) == (111, 222)


async def test_sender_without_users_row_does_not_break_fk(accounts):
    async with account_db(accounts.a) as db:
        chat = await chats.upsert_chat(db, accounts.a, ChatRow(CHAT_EXT, "supergroup"))
        msg = await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=T0, sender_external_id=PEER,
        ))
        await db.commit()
        [row], _ = await messages.get_messages(db, accounts.a)
    assert row.id == msg and row.sender_user_id is None


async def test_snapshot_of_other_account_is_invisible(accounts):
    seeded_b = await _seed(accounts.b, "B")
    async with account_db(accounts.a) as db:
        assert await snapshots.get_snapshot(db, accounts.a, seeded_b["snapshot"]) is None
    async with account_db(accounts.b) as db:
        members, total = await snapshots.list_members(db, accounts.b, seeded_b["snapshot"])
    assert total == 1 and members[0].user_id == seeded_b["user"]


async def test_raw_events_retention_owner_only(accounts):
    await _seed(accounts.a, "A")
    async with account_db(accounts.a) as db:
        with pytest.raises(RuntimeError, match="owner"):
            await raw_events.purge_older_than(db, datetime.now(timezone.utc))
    async with owner_db() as db:
        purged = await raw_events.purge_older_than(db, datetime.now(timezone.utc) + timedelta(minutes=1))
        await db.commit()
    assert purged >= 1


# ---------------------------------------------------------------------------
# T30: audit rotation and audit writes under the app role
# ---------------------------------------------------------------------------

async def test_t30_audit_rotation_and_write_as_app_role(accounts):
    async with accounts.app_engine.connect() as conn:
        created = (await conn.execute(text("SELECT ensure_audit_partition(3)"))).scalar()
        await conn.execute(text("SELECT * FROM drop_old_audit_partitions(90)"))
        await conn.execute(text(
            "INSERT INTO audit_logs (ts, account_id, alias, tool, status) VALUES (now(), :a, 'iso-a', 'iso_test', 'ok')"
        ), {"a": accounts.a})
        await conn.commit()
    assert created.startswith("audit_logs_")
    sql("DELETE FROM audit_logs WHERE tool = 'iso_test'")
