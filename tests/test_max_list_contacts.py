"""MAX list_contacts in STORAGE_MODE=new (Р4 addition, 19.10).

max-work answered list_contacts with 0 while users held 356 rows with
is_contact = true: the route read the client's login list, and PyMax logs in
with the stored contacts_sync marker — MAX then sends only the contacts changed
since (usually none). The address book is the account's users (is_contact).
"""

import pytest
from pymax.types import User

from app.max import normalize
from app.max.store_new import NewMaxStore
from app.repo import users as users_repo
from app.storage import account_db
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import _authorized
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

ANNA = {"id": 200, "names": [{"firstName": "Анна"}], "link": "anna", "phone": 79005556677}
BORIS = {"id": 300, "names": [{"firstName": "Борис", "lastName": "Б"}]}


async def _contacts(env, **params) -> dict:
    resp = await env.client.get("/api/v1/contacts", headers=env.h(), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_list_contacts_reads_the_stored_address_book_not_the_login_delta(new_env):  # noqa: F811
    new_env.server.contacts = [BORIS, ANNA]
    client = await _authorized(new_env)
    # The next login carries contacts_sync: MAX sends no contacts at all.
    client.contacts = []

    body = await _contacts(new_env)
    assert body["total"] == 2
    assert [(c["id"], c["first_name"], c["phone"]) for c in body["items"]] == [
        (200, "Анна", "+79005556677"), (300, "Борис", None)]
    assert set(body["items"][0]) == {"id", "username", "first_name", "last_name", "phone", "is_bot", "is_self"}

    page = await _contacts(new_env, limit=1, offset=1)
    assert (page["total"], [c["id"] for c in page["items"]]) == (2, [300])


@pytest.mark.asyncio
async def test_list_contacts_leaves_out_people_who_are_not_contacts(new_env):  # noqa: F811
    new_env.server.contacts = [ANNA]
    await _authorized(new_env)
    # Seen in a group, not in the address book.
    stranger = User.model_validate({"id": 400, "names": [{"firstName": "Чужой"}]})
    await NewMaxStore(new_env.account_id).upsert_users([normalize.normalize_user(stranger)], "member")
    async with account_db(new_env.account_id) as db:
        rows, total = await users_repo.list_contacts(db, new_env.account_id)
    assert ([u.external_id for u in rows], total) == ([200], 1)
    body = await _contacts(new_env)
    assert ([c["id"] for c in body["items"]], body["total"]) == ([200], 1)
