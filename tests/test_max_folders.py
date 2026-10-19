"""MAX chat folders (plan 2026-10-19 Р4 PR-R4-2, Р-41; QA R4-12).

update_folder merges with the folder as MAX has it: PyMax sends None as [] and a
rename alone would empty the folder.
"""

from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import FOREIGN, _create, _foreign, _get, _patch, _post
from tests.test_max_write_limits import _events, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture


def _writes(w, name: str) -> list[tuple]:
    return [x for x in w.env.server.writes if x[0] == name]


async def test_create_rename_keeps_chats_then_delete(rw):
    gid = await _create(rw, user_ids=())
    await _foreign(rw)
    rw.env.server.folders.append({"id": "work", "title": "Работа", "include": [FOREIGN], "filters": [1],
                                  "options": ["X"]})
    listed = (await _get(rw, "/folders")).json()
    assert listed == {"items": [{"folder_id": "work", "title": "Работа", "chat_ids": [FOREIGN], "filters": [1],
                                 "options": ["X"]}], "order": ["work"]}

    resp = await _post(rw, "/folders", {"title": "R4 тест", "chat_ids": [gid, gid]})
    assert resp.status_code == 200, resp.text
    fid = resp.json()["folder_id"]
    assert resp.json()["chat_ids"] == [gid]

    resp = await _patch(rw, f"/folders/{fid}", {"title": "R4 тест 2"})
    assert resp.status_code == 200 and resp.json()["chat_ids"] == [gid], resp.text
    assert _writes(rw, "update_folder") == [("update_folder", fid, "R4 тест 2", [gid], [], [])]
    resp = await _patch(rw, "/folders/work", {"title": "Работа 2"})
    assert _writes(rw, "update_folder")[-1] == ("update_folder", "work", "Работа 2", [FOREIGN], [1], ["X"])
    resp = await _patch(rw, "/folders/work", {"chat_ids": [gid]})
    assert _writes(rw, "update_folder")[-1] == ("update_folder", "work", "Работа 2", [gid], [1], ["X"])

    resp = await _post(rw, f"/folders/{fid}/delete", {"confirm_title": "R4 тест"})
    assert resp.status_code == 409 and _writes(rw, "delete_folder") == []
    resp = await _post(rw, f"/folders/{fid}/delete", {"confirm_title": "R4 тест 2"})
    assert resp.json() == {"folder_id": fid, "deleted": True}
    assert [f["id"] for f in rw.env.server.folders] == ["work"]
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == [
        "create_folder", "update_folder", "update_folder", "update_folder", "delete_folder"]


async def test_refusals_without_a_frame(rw):
    resp = await _post(rw, "/folders", {"title": "x", "chat_ids": [-1234]})
    assert resp.status_code == 422 and "not chats of this account" in resp.text
    assert (await _post(rw, "/folders", {"title": "x", "chat_ids": list(range(101))})).status_code == 422
    assert (await _patch(rw, "/folders/nope", {"title": "y"})).status_code == 404
    assert (await _patch(rw, "/folders/nope", {})).status_code == 422
    assert (await _post(rw, "/folders/nope/delete", {"confirm_title": "y"})).status_code == 404
    assert rw.env.server.writes == [] and _events(rw.id) == []
