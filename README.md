# TG-MyPerson

MTProto bridge for personal Telegram accounts: PostgreSQL storage, REST API, MCP-compatible.

## Quick Start

```bash
cp .env.example .env
# Fill in TG_API_ID, TG_API_HASH, DATABASE_URL, API_KEY, TG_ADMIN_API_KEY
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Session Management (multi-account)

TG-MyPerson supports multiple Telegram accounts in a single instance. Each account
has an **alias** (e.g. `work`, `personal-ro`) and a **mode** (`rw` = read-write,
`ro` = read-only).

Sessions are stored in the `accounts` + `account_sessions` tables (PostgreSQL).
On startup the pool loads all enabled accounts and connects their Telethon clients.

> **Phase 5 note:** `session_plaintext` in `account_sessions` is stored as plaintext.
> Encryption-at-rest is planned as a separate ticket in `dev-coder` and is not yet
> implemented.

### Authentication headers

| Header | Used for |
|---|---|
| `X-Admin-Key` | Admin endpoints: `POST/GET/PATCH/DELETE /api/v1/accounts/*` |
| `X-API-Key` | All tool endpoints |
| `X-Session-Alias` | Select which account to use (defaults to `work`) |

### Creating a new session (3-step curl example)

**Step 1 — Register the account:**

```bash
curl -s -X POST http://localhost:8000/api/v1/accounts \
  -H "X-Admin-Key: $TG_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"alias": "personal-ro", "phone": "+79001234567", "mode": "ro"}'
```

**Step 2 — Send login code:**

```bash
curl -s -X POST "http://localhost:8000/api/v1/auth/login?session=personal-ro" \
  -H "X-API-Key: $API_KEY"
# Telegram sends a code to the phone
```

**Step 3 — Submit the code:**

```bash
curl -s -X POST "http://localhost:8000/api/v1/auth/code?session=personal-ro" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"code": "12345"}'
# Returns {"status": "authorized", ...}
```

After authorization the session string is persisted automatically in `account_sessions`.

### Legacy bootstrap (deprecated)

Before multi-account (Phase 1-4), the `work` session was bootstrapped via
`TG_SESSION_STRING` / `TG_PHONE_NUMBER` env vars and stored in the `tg_session`
table. That table was removed in migration `004`. Use the 3-step curl flow above
to create or re-authenticate the `work` account.

## Database migrations

```bash
alembic upgrade head     # apply all migrations
alembic downgrade -1     # roll back one step
alembic current          # show current revision
```

Migrations:

| Revision | Description |
|---|---|
| 001 | Initial schema (tg_users, tg_chats, tg_messages, tg_media, tg_session, tg_sync_state) |
| 002 | Multi-account schema (accounts, account_sessions, audit_logs, snapshots, registry) |
| 003 | Fix index on chat_members_snapshots.taken_at (DESC) |
| 004 | Drop legacy tg_session table (Phase 4 cleanup) |
| 005 | Partition rotation SQL helpers for audit_logs |
| 006 | Fix regex in drop_old_audit_partitions to handle timezone-aware bounds |
| 007 | `tg_messages.sender_chat_id` (broadcast senders) |
| 008 | `audit_logs` DEFAULT partition + drain helper |
| 009 | MAX platform: `accounts.platform` (+ write guards), `max_*` tables |

## Environment variables

See `.env.example` for the full list. Required variables:

| Variable | Description |
|---|---|
| `DATABASE_URL` | asyncpg connection string |
| `TG_API_ID` | From https://my.telegram.org |
| `TG_API_HASH` | From https://my.telegram.org |
| `API_KEY` | REST API authentication key |
| `TG_ADMIN_API_KEY` | Admin endpoints authentication key |

## Docker

```bash
docker compose up -d
```

The `docker-compose.yml` starts the app and a PostgreSQL instance. Migrations run
automatically on container start.

## Security

**Warning: database backups expose Telegram sessions.** Until Phase 5
(encryption-at-rest) is implemented, `account_sessions.session_plaintext`
stores raw Telethon `StringSession` strings in plaintext. **Anyone with read
access to a DB dump can fully impersonate the corresponding Telegram account**
— read all messages, send messages, join/leave groups. Treat backups with the
same sensitivity as the Telegram credentials themselves: encrypt at rest
(e.g., `age`), restrict access, never commit to git. Tracked in ticket
`[tg-myperson] Шифрование session at rest (Phase 5)`.

## Operational notes

**Single-worker assumption.** Service caches `alias → account_id/mode` for
10 seconds in process memory. `PATCH is_enabled=false` invalidates this cache
only in the worker that handled the request. Running uvicorn with
`--workers > 1` will cause stale cache for up to 10 seconds in other workers,
allowing disabled accounts to keep working briefly. For production: run with
`--workers 1` (or 1 per container, scale horizontally) until pub/sub-based
invalidation is added.

## Operational maintenance

### audit_logs partition rotation

The `audit_logs` table is partitioned by month. Partition rotation is
**fully self-sufficient** — it runs inside the service itself, with **no
external cron, no sidecar, and no scheduled task of any kind**. See ADR
`_system/docs/architect/2026-06-16-tg-myperson-self-sufficient-partition-lifecycle.md`
in the vault for the full design and rationale.

**How it works (defense-in-depth, all in-process / in-DB):**

- **DEFAULT partition (safety-net).** `audit_logs_default` catches any row
  whose month has no dedicated partition, so an INSERT can **never** fail with
  a partition constraint violation — even at a month boundary.
- **Ensure-on-startup.** On every boot, `lifespan()` calls
  `ensure_partitions()`, which materializes the current + next 2 months and
  drops partitions older than the retention window (90 days), under a
  PostgreSQL advisory lock.
- **Daily in-process loop.** A lightweight `asyncio` task re-runs
  `ensure_partitions()` every 24h, so long-running containers self-heal across
  month boundaries without needing a restart. No APScheduler — plain asyncio.
- **Drain-from-default.** When a new month's partition is created while the
  DEFAULT already holds rows for that month, those rows are redistributed into
  the new partition (only runs when DEFAULT is non-empty).

**Retention policy:** partitions with an upper bound older than 90 days are
dropped automatically by `drop_old_audit_partitions(90)`. The DEFAULT
partition is never dropped (it has no upper bound).

SQL functions (migrations 005/006/008):
- `create_audit_partition(months_ahead int)` — idempotent partition creation
- `drop_old_audit_partitions(retention_days int)` — drops expired partitions
- `drain_audit_default_for_month(target_start date)` — redistributes rows out
  of DEFAULT into a month partition (migration 008)

**Manual fallback (debugging only):**

```bash
python -m app.scripts.audit_partitions
```

This script is no longer the operational mechanism — rotation happens
automatically in-process. It exists only as a manual escape hatch.

> **Note (2026-06-16):** the previous manual/cron approaches (ClaudeClaw job,
> sidecar `psql` container, Coolify Scheduled Task, pg_cron/pg_partman) were
> all rejected — the service now owns its own partition lifecycle end-to-end.
> The DB image is `postgres:16-alpine`.

## API overview

- `GET /api/v1/healthz` — health check (no auth)
- `GET /api/v1/readyz` — readiness (DB + work session)
- `GET/POST/PATCH/DELETE /api/v1/accounts/*` — account management (X-Admin-Key)
- `GET /api/v1/chats` — list chats
- `GET /api/v1/messages` — list messages
- `POST /api/v1/messages/send` — send message (rw mode only)
- `GET /api/v1/auth/status` — session auth status
- `POST /api/v1/auth/login` — send Telegram login code
- `POST /api/v1/auth/code` — confirm login code
- `POST /api/v1/auth/logout` — log out session

## Платформа MAX

Второй мессенджер в том же сервисе (ADR `_system/docs/architect/2026-10-07-tg-myperson-max-adr.md`
в vault). Транспорт — [PyMax](https://github.com/MaxApiTeam/PyMax) (`maxapi-python`, точный пин).
Фаза 1 — только чтение; запись (фаза 2) — по решению владельца.

### Как устроено

- **Платформа определяется алиасом.** `accounts.platform` = `telegram` | `max`. Те же URL, что у TG;
  отличается только `X-Session-Alias` (например `max-work`). Middleware `platform_dispatch` переписывает
  путь MAX-запроса на внутренний роутер `/api/v1/_max/…` с теми же именами роутов, поэтому каталог тулов,
  ro-проверка и аудит работают как у TG. Снаружи `/api/v1/_max/…` → 404.
- **Данные — отдельные таблицы `max_*`** той же формы, что `tg_*` (колонка в колонку). Telegram-таблицы,
  TG-код и `readyz` MAX не трогает. У MAX-ответа по сообщению есть доп. поле `deleted_at`.
- **Тул, не реализованный для MAX, → 501** `{"error","tool","platform":"max","alias"}`; запись на ro → 403
  раньше 501. MAX-only тулы (`/auth/qr*`) для TG-алиаса → 501.
- **Адаптер `app/max/`.** `import pymax` разрешён только в `session.py`, `store.py`, `auth.py`,
  `normalize.py`, `media.py` (сторож `test_s16_pymax_import_boundary`).
  - `session.py` — клиент PyMax с жёстко зашитыми `relogin=False`, `telemetry=False`, `interactive=False`,
    без `RegistrationConfig`, с прокси и нашим хранилищем; машина состояний и supervisor
    (backoff 5 с → 10 мин). Отозванный токен → `unauthorized`, признаки бана → `banned`: повторных входов нет.
  - `store.py` — сессия PyMax в `account_sessions.session_plaintext` как JSON v1, файлов в контейнере нет.
  - `ingest.py` — каждый кадр сообщений/правок/удалений/чатов сначала пишется в `max_raw_events`,
    потом разбирается; кадр, который PyMax не смог разобрать, остаётся там с `normalized=false`.
    PyMax обрабатывает каждый кадр в своей задаче; кадры одного чата идут через замок чата в порядке
    прихода (удаление никогда не обгоняет вставку), разные чаты — параллельно.
  - `sync.py` — backfill (страницы по 100, пауза 1,5 с) и добор пропусков после каждого (пере)подключения.
    Курсор `max_sync_state.newest_time_ms` значит «до сюда всё есть без дыр»: его двигает история (добор,
    backfill), а живое событие — только в чате, уже догнанном в текущем подключении. До этого живое
    сообщение сохраняется, но курсор не трогает и строку `max_sync_state` не создаёт, поэтому пропуск
    за время простоя всегда добирается, а новый чат получает затравку.
- **MAX пишет все чаты.** `is_monitored` (`PATCH /chats/{id}`) хранится, но приём и добор не ограничивает.
  - `media.py` — файлы по запросу, через тот же прокси.
- **Прокси обязателен** (`MAX_REQUIRE_PROXY=true`): без `MAX_PROXY_URL` MAX-сессия не стартует.
- **Чтение не ставит «прочитано»**: история запрашивается с `interactive=False`, read/presence не вызываются.

### Переменные окружения

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `MAX_ENABLED` | `false` | поднимать MAX-пул |
| `MAX_PROXY_URL` | — | `socks5://…` или `http://…`; весь трафик MAX, включая медиа. Прод: `socks5://max-egress:1080` |
| `MAX_REQUIRE_PROXY` | `true` | без прокси сессия не стартует |
| `MAX_CATCHUP_SEED` | `50` | сколько последних сообщений брать у впервые увиденного чата |
| `MAX_CATCHUP_MAX_CHATS` | `60` | чатов за проход добора; остаток — через 15 мин |
| `MAX_RAW_EVENTS_RETENTION_DAYS` | `30` | хранение `max_raw_events` |
| `MAX_WRITE_RATE_DEFAULT` | `20` | фаза 2: лимит отправок в час |

### Вход: QR + пароль 2FA (runbook)

Переменные для примеров: `U=https://tg-myperson.scom-it.ru/api/v1`, `$API_KEY`, `$TG_ADMIN_API_KEY`.
QR сканируется телефоном с **другого экрана** (браузер на Mac): QR на экране того же телефона не отсканировать.

**0. Аккаунт** (один раз):

```bash
curl -s -X POST "$U/accounts" -H "X-Admin-Key: $TG_ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"alias":"max-work","phone":"+79XXXXXXXXX","mode":"ro","platform":"max"}'
curl -s "$U/accounts" -H "X-Admin-Key: $TG_ADMIN_API_KEY" | jq '.[] | select(.alias=="max-work") | .runtime'
# runtime.proxy должен быть true; state — stopped (сессии ещё нет)
```

**1. Если `runtime.state` = `error` или `banned` — сначала logout.** Пока в БД лежит сохранённая сессия,
новый вход отвечает `409 {"error":"stored session exists; POST /auth/logout first"}`:

```bash
curl -s -X POST "$U/auth/logout" -H "X-API-Key: $API_KEY" -H "X-Session-Alias: max-work"
```

При `banned` сначала разобраться, почему (`runtime.last_error`), а не входить заново сразу.
При `unauthorized` (токен отозван) сессия уже деактивирована — logout не нужен.

**2. Запросить QR:**

```bash
curl -s -X POST "$U/auth/qr" -H "X-API-Key: $API_KEY" -H "X-Session-Alias: max-work"
# 202 {"status":"awaiting_qr","qr_link":"https://max.ru/:auth/…","expires_at":"…","qr_png_url":"/api/v1/auth/qr.png?session=max-work"}
```

Открыть в браузере на Mac `https://tg-myperson.scom-it.ru/api/v1/auth/qr.png?session=max-work&api_key=<API_KEY>`
и отсканировать телефоном: **MAX → Профиль → Устройства → Войти по QR**. QR живёт до `expires_at`;
истёк — `GET /auth/qr` вернёт `"status":"expired"`, повторить шаг 2 (новый QR). Повторный `POST /auth/qr`,
пока QR жив, возвращает тот же QR.

**3. Дождаться запроса пароля** (на `max-work` включена 2FA):

```bash
curl -s "$U/auth/qr" -H "X-API-Key: $API_KEY" -H "X-Session-Alias: max-work"
# {"status":"awaiting_password","password_hint":"…", …}
```

**4. Отправить пароль:**

```bash
curl -s -X POST "$U/auth/code" -H "X-API-Key: $API_KEY" -H "X-Session-Alias: max-work" \
  -H "Content-Type: application/json" -d '{"code":"","password":"<пароль>"}'
# 200 {"status":"authorized","alias":"max-work","user_id":…,"username":…}
# 400 {"error":"invalid password","attempts_left":2} — неверный, можно повторить
```

Не больше **3 попыток** и **10 минут** с первого запроса пароля. Потом вход завершается: `state=error`,
соединение закрыто, повторов нет — начать с шага 2 (сессия при неудачном входе не сохраняется,
logout не нужен). Лимит держит наш провайдер: в PyMax 2.4.1 QR-вход переспрашивает пароль бесконечно.

**5. Проверить:**

```bash
curl -s "$U/auth/status" -H "X-API-Key: $API_KEY" -H "X-Session-Alias: max-work"   # connected: true
curl -s "$U/accounts" -H "X-Admin-Key: $TG_ADMIN_API_KEY" | jq '.[] | select(.alias=="max-work") | .runtime'
# state=authorized, transport=web, last_event_at обновляется, catchup_backlog_chats → 0
```

Резервный путь — SMS (`POST /auth/login`, затем `POST /auth/code {"code":"…"}`, при 2FA ответ
`2fa_required` и повтор с `password`); транспорт TCP. Неверный SMS-код завершает вход (PyMax не
переспрашивает код) — заново `POST /auth/login`. Транспорт фиксируется при входе и не меняется до нового входа.

### Бэкап и восстановление сессии

Экспорта через API нет. Бэкап (infra-ops, только чтение):

```sql
SELECT s.session_plaintext FROM account_sessions s JOIN accounts a ON a.id = s.account_id
WHERE a.alias = 'max-work' AND s.is_active;
```

JSON сохранить в `secrets.env.age`. Восстановление — `POST /auth/session {"session_string":"<JSON>"}`:
сессия сначала логинится в MAX и только при успехе пишется в БД; при ошибке прежняя сессия не трогается.

### Ограничения фазы 1

- **Удаления за время простоя не видны** (протокол их не отдаёт); правки подтягиваются добором.
- **Поиска на сервере MAX нет**: `search_global` ищет по нашей БД (`ILIKE` по тексту).
- **Голосовые и кружки**: PyMax 2.4.1 иногда отвечает `video.not.ready` → `502`, не `500`.
- `list_members`, запись и прочие тулы фазы 2/3 → `501` (запись на ro → `403`).
- `text_html` = `NULL` (разметка — в `raw_data.elements`); у вложений MAX нет MIME-типа.
- Коды бана MAX заранее неизвестны: `banned` — эвристика по словам `ban/blocked/suspended/restricted`
  (`app/max/errors.py`), уточнить по живым ошибкам.
- Границы `fetch_history` и маркер `fetch_chats` выведены из кода PyMax: на первом живом прогоне
  сверить по логам, что backfill и добор не теряют сообщение на границе страницы.

### Обновление PyMax

PyMax ходит в недокументированный протокол MAX; модели и поведение меняются между релизами.
Пин точный (`maxapi-python==…` в `pyproject.toml`), обновление — только так:

1. Прочитать changelog и открытые issues PyMax между текущей и новой версией. Отдельно проверить места,
   на которые опирается адаптер: цикл `BaseClient.start()` (мы его не используем — supervisor вызывает
   `connect()`), порядок диспетчера и `App.on_event` (frame hook), `QrAuthFlow` (пароль), передачу
   `interactive`/`telemetry`/`relogin` в `ClientConfig`, `fetch_history`/`fetch_chats`.
2. Поднять пин в `pyproject.toml`.
3. Прогнать `pytest`: нормализация на фикстурах (`tests/fixtures/max/`) ловит смену моделей,
   `test_s13_pymax_config_guards` — защитные настройки, `test_frame_hook_sees_frames_pymax_cannot_parse` —
   журнал сырых кадров, `test_s4_*` — лимит пароля.
4. Деплой — только через infra-ops.
5. После деплоя проверить: `runtime.state=authorized`, `last_event_at` свежий, число
   `SELECT count(*) FROM max_raw_events WHERE NOT normalized` не растёт.
6. Если пока PyMax был сломан, в базе осталась дыра — `POST /sync/backfill {"chat_id":…,"direction":"forward"}`
   по затронутым чатам (добор пропусков сделает это сам при следующем подключении). Кадры с
   `normalized=false` за этот период лежат в `max_raw_events` (30 дней).

### FastAPI

`fastapi<0.137` в `pyproject.toml`: начиная с 0.137 подключённые роутеры завёрнуты в приватный
`_IncludedRouter`. Резолвер тулов уже от этого не зависит (плоская таблица `app/authz/route_table.py`,
сервис не стартует, если ключевые роуты не резолвятся), но снимать ограничение — отдельным PR после
прогона тестов на новой версии.
