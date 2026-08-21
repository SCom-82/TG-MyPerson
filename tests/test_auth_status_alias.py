"""auth_status обязан резолвить тот же алиас, что и остальные эндпоинты.

Регрессия: /auth/status читал алиас ТОЛЬКО из query-параметра ?session=, игнорируя
request.state.session_alias, который middleware ставит по заголовку X-Session-Alias.
Поэтому запрос с `X-Session-Alias: personal-ro` и без ?session= молча уходил в
дефолтный "work" и возвращал личность чужого аккаунта — при том, что /auth/me на
тех же самых заголовках отдавал personal-ro. Из-за этого в документации годами
висела оговорка «auth_status ВРЁТ для алиасных сессий».
"""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client_capturing_pool_alias():
    """Клиент + список алиасов, с которыми реально дёрнули pool.get()."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    seen: list[str] = []

    async def _capturing_get(alias: str):
        seen.append(alias)
        session = MagicMock()
        # Ответ должен проходить валидацию AuthStatusResponse, иначе FastAPI
        # поднимет ResponseValidationError раньше, чем тест дойдёт до проверки.
        session.get_auth_status = AsyncMock(
            return_value={
                "connected": True,
                "phone_number": f"+phone-{alias}",
                "user_id": 111,
                "username": alias,
            }
        )
        # /auth/me на том же стабе — чтобы сравнить резолвинг алиаса.
        # Именно SimpleNamespace, а не MagicMock: у мока getattr отдаёт мок,
        # и AuthMeResponse падает на валидации строковых полей.
        me = SimpleNamespace(
            id=111, username=alias, first_name=alias, last_name=None,
            phone=f"+phone-{alias}", premium=False, verified=False,
            bot=False, dc_id=2, lang_code="ru",
        )
        session.client = MagicMock()
        session.client.is_user_authorized = AsyncMock(return_value=True)
        session.client.get_me = AsyncMock(return_value=me)
        return session

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="ro")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)), \
         patch.object(pool_module.pool, "get", AsyncMock(side_effect=_capturing_get)):

        importlib.reload(main_module)
        mw._alias_cache.clear()
        mw._mode_cache.clear()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, seen


@pytest.mark.asyncio
async def test_header_alias_is_honoured(client_capturing_pool_alias):
    """X-Session-Alias без ?session= — главный сломанный кейс."""
    client, seen = client_capturing_pool_alias
    await client.get(
        "/api/v1/auth/status",
        headers={"x-api-key": "test-api-key", "x-session-alias": "personal-ro"},
    )
    assert seen, "auth_status не дошёл до pool.get()"
    assert seen[-1] == "personal-ro", (
        f"auth_status взял алиас '{seen[-1]}' вместо 'personal-ro' из заголовка — "
        "снова читает только query-параметр"
    )


@pytest.mark.asyncio
async def test_query_param_still_works(client_capturing_pool_alias):
    """Обратная совместимость: ?session= по-прежнему работает."""
    client, seen = client_capturing_pool_alias
    await client.get(
        "/api/v1/auth/status?session=personal-ro",
        headers={"x-api-key": "test-api-key"},
    )
    assert seen[-1] == "personal-ro"


@pytest.mark.asyncio
async def test_defaults_to_work_without_alias(client_capturing_pool_alias):
    """Без заголовка и без параметра — дефолт 'work', как и было."""
    client, seen = client_capturing_pool_alias
    await client.get("/api/v1/auth/status", headers={"x-api-key": "test-api-key"})
    assert seen[-1] == "work"


@pytest.mark.asyncio
async def test_status_and_me_resolve_same_alias(client_capturing_pool_alias):
    """Главное свойство: status и me на одних заголовках берут один аккаунт."""
    client, seen = client_capturing_pool_alias
    headers = {"x-api-key": "test-api-key", "x-session-alias": "personal-ro"}

    await client.get("/api/v1/auth/status", headers=headers)
    status_alias = seen[-1]

    await client.get("/api/v1/auth/me", headers=headers)
    me_alias = seen[-1]

    assert status_alias == me_alias == "personal-ro", (
        f"status резолвит '{status_alias}', me — '{me_alias}'; "
        "именно это расхождение и делало auth_status лжецом"
    )
