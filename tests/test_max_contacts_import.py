"""MAX import_contacts, remove_contact, delete_dialog (plan 2026-10-19 Р4 PR-R4-2, Р-38, Р-42, Р-43;
QA R4-10, R4-11).

The import is our own SYNC frame (PyMax's payload) that keeps the phones map:
which number gave which user.
"""

import logging

from app.max import frames
from app.repo import chats
from app.repo.rows import ChatRow
from app.storage import account_db
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import _chat_row, _create, _post
from tests.test_max_ingest import ME
from tests.test_max_write_limits import _events, _limits, _status, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

ANNA, BORIS = "+79001110001", "+79001110002"


def _book(w) -> None:
    w.env.server.phone_book.update({
        ANNA: {"id": 6001, "names": [{"firstName": "Анна"}], "phone": 79001110001},
        BORIS: {"id": 6002, "names": [{"firstName": "Борис"}]},  # MAX hides the phone
    })


async def _import(w, *phones, refresh=False):
    return await _post(w, "/contacts/import", {"contacts": [{"phone": p, "first_name": f"N{i}"}
                                                            for i, p in enumerate(phones)], "refresh": refresh})


def test_sync_frame_is_pymax_import_contacts():
    assert frames.sync_frame([(ANNA, "Анна")]) == {"contactList": {ANNA: {"firstName": "Анна"}}}


async def test_import_maps_numbers_by_the_phones_map(rw, caplog):
    _book(rw)
    caplog.set_level(logging.INFO)
    resp = await _import(rw, ANNA, "8 (900) 111-00-02", "+79001110003", ANNA)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(i["phone"], i["found"], i["cached"], i["user_id"]) for i in body["items"]] == [
        (ANNA, True, False, 6001), (BORIS, True, False, 6002), ("+79001110003", False, False, None)]
    assert (body["found"], body["not_found"], body["cached"], body["imports_left_today"]) == (2, 1, 0, 47)
    anna = body["items"][0]
    assert anna["chat_id"] == ME ^ 6001 and anna["person_id"] is not None and anna["name"] == "Анна"
    assert rw.env.server.sync_calls == [{ANNA: {"firstName": "N0"}, BORIS: {"firstName": "N1"},
                                         "+79001110003": {"firstName": "N2"}}]
    assert [e[0] for e in _events(rw.id)] == ["contact_import"] * 3
    contacts = (await rw.http.get("/api/v1/contacts", headers=rw.env.h())).json()
    assert {c["id"] for c in contacts["items"]} >= {6001, 6002}
    assert "1110001" not in caplog.text and "+7******0001" in caplog.text  # masked in the log

    resp = await _import(rw, ANNA)  # known now: from the base, no SYNC, no slot
    assert resp.json()["items"][0]["cached"] is True and len(rw.env.server.sync_calls) == 1
    resp = await _import(rw, ANNA, refresh=True)
    assert resp.json()["items"][0]["cached"] is False and len(rw.env.server.sync_calls) == 2
    assert (await _status(rw))["counters"]["contacts_imported_last_day"] == 4


async def test_phones_map_matches_a_user_without_a_phone(rw):
    rw.env.server.sync_answer = {"contacts": [{"id": 6005, "names": [{"firstName": "Скрыт"}]}],
                                 "phones": {"79001110005": 6005}}
    body = (await _import(rw, "+79001110005")).json()
    assert body["items"][0]["user_id"] == 6005 and body["found"] == 1


async def test_import_refusals_before_max(rw):
    resp = await _post(rw, "/contacts/import", {"contacts": [{"phone": f"+7900111{i:04}", "first_name": "x"}
                                                             for i in range(21)]})
    assert resp.status_code == 422
    resp = await _import(rw, ANNA, "12")
    assert resp.status_code == 422 and "country code" in resp.text
    assert rw.env.server.sync_calls == [] and _events(rw.id) == []

    for start in (0, 20):
        assert (await _import(rw, *[f"+7900200{start + i:04}" for i in range(20)])).status_code == 200
    assert (await _import(rw, *[f"+7900300{i:04}" for i in range(8)])).status_code == 200
    resp = await _import(rw, "+79004000001", "+79004000002", "+79004000003")
    assert resp.status_code == 429 and resp.json()["limit"] == "contacts_imported_per_day", resp.text
    assert len(_events(rw.id)) == 48 and len(rw.env.server.sync_calls) == 3

    await _limits(rw, contacts_imported_per_day=100)
    rw.env.server.group_errors = [TimeoutError("no answer")]
    resp = await _import(rw, "+79004000001")
    assert resp.status_code == 504 and "check list_contacts" in resp.text
    assert len(_events(rw.id)) == 49  # kept: the numbers may have been imported


async def test_remove_contact(rw):
    _book(rw)
    await _import(rw, ANNA)
    assert (await _post(rw, "/contacts/424242/remove")).status_code == 404
    resp = await _post(rw, "/contacts/6001/remove")
    assert resp.status_code == 200 and resp.json() == {"user_id": 6001, "is_contact": False}, resp.text
    assert rw.env.server.removed_contacts == [6001]
    assert sql("SELECT is_contact FROM users WHERE account_id = %s AND external_id = 6001", (rw.id,),
               fetch=True) == [(False,)]
    assert [e[:2] for e in _events(rw.id)][-1] == ("action", "remove_contact")
    contacts = (await rw.http.get("/api/v1/contacts", headers=rw.env.h())).json()
    assert 6001 not in {c["id"] for c in contacts["items"]}


async def test_delete_dialog_only_a_private_chat_confirmed_by_the_peer(rw):
    peer = 6001
    dialog = ME ^ peer
    async with account_db(rw.id) as db:
        await chats.upsert_chat(db, rw.id, ChatRow(dialog, "private", title="Анна"))
        await chats.upsert_chat(db, rw.id, ChatRow(0, "private", title="Избранное"))
        await db.commit()
    gid = await _create(rw, user_ids=())
    assert (await _post(rw, f"/chats/{gid}/delete", {"confirm_user_id": peer})).status_code == 422
    assert (await _post(rw, "/chats/0/delete", {"confirm_user_id": ME})).status_code == 422
    assert (await _post(rw, f"/chats/{dialog}/delete", {"confirm_user_id": 777})).status_code == 409
    assert [c for c in rw.env.server.group_calls if c[0] == "delete_chat"] == []

    resp = await _post(rw, f"/chats/{dialog}/delete", {"confirm_user_id": str(peer)})
    assert resp.status_code == 200 and resp.json() == {"chat_id": dialog, "deleted": True, "for_all": False}
    assert [c for c in rw.env.server.group_calls if c[0] == "delete_chat"] == [("delete_chat", dialog, False)]
    row = await _chat_row(rw.id, dialog)
    assert row is not None and row.is_monitored is False  # the history stays
    assert [e[:2] for e in _events(rw.id)][-1] == ("action", "delete_dialog")
