"""migrate_isolation probe (ТЗ 2026-10-13 §8.1, PR-0).

AC: on the test DB with a fake client it fills mig_membership; it writes
nothing but mig_membership; Telegram — only get_me / iter_dialogs. Review
13.10 (P0-1): in the service, on the pool's clients, never connect/disconnect
them; own connections only with --service-stopped.

Live DB. probe() commits through app.database.async_session, so the accounts
created here are committed and removed by the fixture; assertions look only at
these accounts (the test DB may hold others).
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from telethon.tl.types import (
    Channel,
    ChatAdminRights,
    ChatPhotoEmpty,
    User,
)
from telethon.tl.types import Chat as BasicGroup

from app.scripts import migrate_isolation as mi
from tests.conftest import _TestSessionFactory

DATE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ALIASES = ("probe-work", "probe-ro", "probe-nosession", "probe-max")
ORPHAN_CHAT = -1_009_300_000_001
SHARED_CHANNEL = 9_300_000_010


def _channel(cid, title, *, broadcast=False, creator=False, admin=False, min_=False, access_hash=111):
    return Channel(
        id=cid, title=title, photo=ChatPhotoEmpty(), date=DATE,
        broadcast=broadcast, megagroup=not broadcast, creator=creator, min=min_,
        admin_rights=ChatAdminRights(post_messages=True) if admin else None,
        access_hash=access_hash,
    )


def _dialogs_work():
    return [
        User(id=9_300_000_100, first_name="Peer", access_hash=555),
        _channel(SHARED_CHANNEL, "Shared supergroup", creator=True, access_hash=777),
        _channel(9_300_000_011, "Own channel", broadcast=True, admin=True, access_hash=888),
        BasicGroup(id=9_300_000_012, title="Basic group", photo=ChatPhotoEmpty(),
                   participants_count=3, date=DATE, version=1),
    ]


def _dialogs_ro():
    return [
        _channel(SHARED_CHANNEL, "Shared supergroup", access_hash=999),
        _channel(9_300_000_013, "Seen as min", min_=True, access_hash=123),
    ]


class FakeClient:
    """Records every attribute access; offers write methods that must stay unused."""

    def __init__(self, me_id, dialogs, *, authorized=True, fail=False):
        self.me_id, self.dialogs, self.authorized, self.fail = me_id, dialogs, authorized, fail
        self.calls: list[str] = []

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        self.calls.append("is_user_authorized")
        return self.authorized

    async def get_me(self):
        self.calls.append("get_me")
        return SimpleNamespace(id=self.me_id)

    async def iter_dialogs(self):
        self.calls.append("iter_dialogs")
        for i, entity in enumerate(self.dialogs):
            if self.fail and i == 1:
                raise ConnectionError("network down")
            yield SimpleNamespace(entity=entity)

    async def send_message(self, *a, **kw):  # pragma: no cover - must not be reached
        self.calls.append("send_message")

    async def __call__(self, request):  # pragma: no cover - must not be reached
        self.calls.append(f"raw:{type(request).__name__}")


async def _exec(sql, **params):
    async with _TestSessionFactory() as s:
        result = await s.execute(text(sql), params)
        await s.commit()
        return result


async def _cleanup():
    ids = "SELECT id FROM accounts WHERE alias = ANY(:a)"
    await _exec(f"DELETE FROM mig_membership WHERE account_id IN ({ids})", a=list(ALIASES))
    await _exec(f"DELETE FROM account_sessions WHERE account_id IN ({ids})", a=list(ALIASES))
    await _exec("DELETE FROM accounts WHERE alias = ANY(:a)", a=list(ALIASES))
    await _exec("DELETE FROM tg_chats WHERE id = :c", c=ORPHAN_CHAT)


@pytest_asyncio.fixture
async def accounts():
    await _cleanup()
    ids = {}
    for alias, platform, session in (
        ("probe-work", "telegram", "S1"),
        ("probe-ro", "telegram", "S2"),
        ("probe-nosession", "telegram", None),
        ("probe-max", "max", "S4"),
    ):
        acc_id = (await _exec(
            "INSERT INTO accounts (alias, phone, mode, platform) VALUES (:a, '+7', 'ro', :p) RETURNING id",
            a=alias, p=platform,
        )).scalar_one()
        ids[alias] = acc_id
        if session:
            await _exec(
                "INSERT INTO account_sessions (account_id, session_plaintext) VALUES (:i, :s)",
                i=acc_id, s=session,
            )
    await _exec(
        "INSERT INTO tg_chats (id, chat_type, title) VALUES (:c, 'supergroup', 'Nobody here')",
        c=ORPHAN_CHAT,
    )
    yield ids
    await _cleanup()


def _factory(clients: dict):
    """Source like probe_in_service: an existing client, wrapped read-only."""
    async def source(account, session_string):
        if not session_string:
            return None
        return mi.ReadOnlyClient(clients.get(session_string) or FakeClient(1, [], authorized=False))
    return source


async def _rows(account_id):
    result = await _exec(
        "SELECT external_chat_id, chat_type, title, role, access_hash FROM mig_membership"
        " WHERE account_id = :a ORDER BY external_chat_id", a=account_id,
    )
    return [tuple(r) for r in result.all()]


async def test_probe_fills_membership(accounts):
    work = FakeClient(7353998268, _dialogs_work())
    ro = FakeClient(219309009, _dialogs_ro())

    report = await mi.probe(_factory({"S1": work, "S2": ro}))

    shared = -1_000_000_000_000 - SHARED_CHANNEL
    assert await _rows(accounts["probe-work"]) == sorted([
        (shared, "supergroup", "Shared supergroup", "creator", 777),
        (-1_009_300_000_011, "channel", "Own channel", "admin", 888),
        (-9_300_000_012, "group", "Basic group", "member", None),
        (9_300_000_100, "private", "Peer", None, 555),
    ])
    assert await _rows(accounts["probe-ro"]) == sorted([
        (shared, "supergroup", "Shared supergroup", "member", 999),
        (-1_009_300_000_013, "supergroup", "Seen as min", "member", None),  # min → no hash
    ])

    acc = report["accounts"]
    assert acc["probe-work"]["status"] == "ok"
    assert acc["probe-work"]["external_user_id"] == 7353998268
    assert acc["probe-work"]["dialogs"] == 4
    assert acc["probe-work"]["by_type"] == {"private": 1, "supergroup": 1, "channel": 1, "group": 1}
    assert acc["probe-ro"]["with_access_hash"] == 1
    assert acc["probe-nosession"]["status"] == "no_session"
    assert "probe-max" not in acc  # TG only
    assert report["shared_chats"].get("supergroup", 0) >= 1
    orphans = report["tg_chats_without_member"]
    assert ORPHAN_CHAT in [o["chat_id"] for o in orphans["top_by_messages"]]
    assert orphans["total"] >= 1


async def test_probe_uses_only_read_calls(accounts):
    work = FakeClient(1, _dialogs_work())
    ro = FakeClient(2, _dialogs_ro())

    await mi.probe(_factory({"S1": work, "S2": ro}))

    for client in (work, ro):
        assert set(client.calls) <= {"is_user_authorized", "get_me", "iter_dialogs"}


async def test_read_only_client_blocks_everything_else():
    client = mi.ReadOnlyClient(FakeClient(1, []))
    for name in ("send_message", "connect", "disconnect"):
        with pytest.raises(PermissionError):
            getattr(client, name)
    with pytest.raises(PermissionError):
        await client(object())
    with pytest.raises(PermissionError):
        client.session = None


async def test_probe_writes_nothing_but_membership(accounts):
    tables = [
        r[0] for r in (await _exec(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
            " AND c.relname <> 'mig_membership'"
        )).all()
    ]

    async def snapshot():
        counts = {}
        for t in tables:
            counts[t] = (await _exec(f'SELECT count(*), md5(string_agg(x::text, \'\' ORDER BY x::text)) FROM "{t}" x')).one()
        return counts

    before = await snapshot()
    await mi.probe(_factory({"S1": FakeClient(7353998268, _dialogs_work()),
                             "S2": FakeClient(2, _dialogs_ro())}))
    after = await snapshot()

    assert before == after
    assert (await _exec(
        "SELECT tg_user_id FROM accounts WHERE id = :i", i=accounts["probe-work"]
    )).scalar() is None


async def test_probe_is_idempotent_and_reflects_left_chats(accounts):
    await mi.probe(_factory({"S1": FakeClient(1, _dialogs_work()), "S2": FakeClient(2, _dialogs_ro())}))
    first = await _rows(accounts["probe-work"])
    await mi.probe(_factory({"S1": FakeClient(1, _dialogs_work()), "S2": FakeClient(2, _dialogs_ro())}))
    assert await _rows(accounts["probe-work"]) == first

    await mi.probe(_factory({"S1": FakeClient(1, _dialogs_work()[:2]), "S2": FakeClient(2, _dialogs_ro())}))
    assert len(await _rows(accounts["probe-work"])) == 2


async def test_failed_account_keeps_previous_rows_and_does_not_stop_others(accounts):
    await mi.probe(_factory({"S1": FakeClient(1, _dialogs_work()), "S2": FakeClient(2, _dialogs_ro())}))
    before = await _rows(accounts["probe-work"])

    failing = FakeClient(1, _dialogs_work(), fail=True)
    report = await mi.probe(_factory({"S1": failing, "S2": FakeClient(2, _dialogs_ro()[:1])}))

    assert report["accounts"]["probe-work"]["status"] == "error"
    assert "network down" in report["accounts"]["probe-work"]["error"]
    assert await _rows(accounts["probe-work"]) == before
    assert len(await _rows(accounts["probe-ro"])) == 1
    assert report["ok"] is False


async def test_not_authorized_account(accounts):
    report = await mi.probe(_factory({
        "S1": FakeClient(1, _dialogs_work(), authorized=False),
        "S2": FakeClient(2, _dialogs_ro()),
    }))

    assert report["accounts"]["probe-work"]["status"] == "not_authorized"
    assert await _rows(accounts["probe-work"]) == []


async def test_probe_in_service_uses_pool_clients(accounts, monkeypatch):
    work, ro = FakeClient(7, _dialogs_work()), FakeClient(8, _dialogs_ro())
    by_alias = {"probe-work": work, "probe-ro": ro}

    async def get(alias):
        return SimpleNamespace(client=by_alias.get(alias))

    monkeypatch.setattr("app.telegram.pool.pool", SimpleNamespace(get=get))
    report = await mi.probe_in_service()

    assert report["accounts"]["probe-work"]["dialogs"] == 4
    assert report["accounts"]["probe-nosession"]["status"] == "no_session"
    for client in (work, ro):
        assert "connect" not in client.calls and "disconnect" not in client.calls


async def test_service_stopped_owns_its_connections(accounts, monkeypatch):
    made = {}

    def factory(account, session_string):
        made[session_string] = FakeClient(account.id, _dialogs_ro())
        return made[session_string]

    monkeypatch.setattr(mi, "telethon_client", factory)
    report = await mi.probe_service_stopped()

    assert report["accounts"]["probe-ro"]["status"] == "ok"
    for client in (made["S1"], made["S2"]):
        assert client.calls[0] == "connect" and client.calls[-1] == "disconnect"


def test_service_running_detection(tmp_path):
    (tmp_path / "17").mkdir()
    (tmp_path / "17" / "cmdline").write_bytes(b"/usr/local/bin/uvicorn\0app.main:app\0--host\0")
    (tmp_path / "18").mkdir()
    (tmp_path / "18" / "cmdline").write_bytes(b"python\0-m\0app.scripts.migrate_isolation\0")
    assert mi.service_running(tmp_path)
    (tmp_path / "17" / "cmdline").write_bytes(b"sleep\0")
    assert not mi.service_running(tmp_path)


def test_cli_service_stopped_refused_while_service_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(mi, "service_running", lambda: True)
    with pytest.raises(SystemExit, match="refusing"):
        mi.main(["probe", "--service-stopped", "--out-dir", str(tmp_path)])


def test_cli_calls_the_service_endpoint(tmp_path, monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"command": "probe", "ok": True, "accounts": {}}).encode()

    def urlopen(req, timeout):
        seen["url"], seen["key"], seen["method"] = req.full_url, req.get_header("X-admin-key"), req.method
        return _Resp()

    monkeypatch.setattr(mi.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mi.settings, "admin_api_key", "adm")

    assert mi.main(["probe", "--out-dir", str(tmp_path), "--url", "http://svc:8000"]) == 0
    assert seen == {"url": "http://svc:8000/api/v1/admin/isolation/probe", "key": "adm", "method": "POST"}
    [report_file] = tmp_path.glob("migrate_isolation-probe-*.json")
    assert json.loads(report_file.read_text())["command"] == "probe"


def test_cli_exit_code_on_failure(tmp_path, monkeypatch):
    monkeypatch.setitem(mi.COMMANDS, "probe", lambda args: {"command": "probe", "ok": False})
    assert mi.main(["probe", "--out-dir", str(tmp_path)]) == 1


async def test_probe_endpoint_requires_admin_key(test_client, monkeypatch):
    import app.config

    async def fake():
        return {"command": "probe", "ok": True}

    monkeypatch.setattr(mi, "probe_in_service", fake)
    # other suites reload the settings with an empty admin key
    monkeypatch.setattr(app.config.settings, "admin_api_key", "test-admin-key")
    assert (await test_client.post("/api/v1/admin/isolation/probe")).status_code == 401
    assert (await test_client.post("/api/v1/admin/isolation/probe",
                                   headers={"x-api-key": "test-api-key"})).status_code == 401
    resp = await test_client.post("/api/v1/admin/isolation/probe", headers={"x-admin-key": "test-admin-key"})
    assert resp.status_code == 200 and resp.json()["command"] == "probe"
