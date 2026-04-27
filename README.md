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

The `audit_logs` table is partitioned by month. Partitions must be created
**before** the month begins, otherwise INSERTs will fail with a partition
constraint violation.

**Retention policy:** partitions with an upper bound older than 90 days are
dropped automatically.

**How to run:**

```bash
python -m app.scripts.audit_partitions
```

The script calls two SQL functions installed by migration 005:
- `create_audit_partition(months_ahead int)` — idempotent, safe to run multiple times
- `drop_old_audit_partitions(retention_days int)` — drops expired partitions

**Automated schedule:** a ClaudeClaw cron job on the server runs this script on
the 1st of each month at 02:00 UTC. Tracked in ticket
`[tg-myperson] partition rotation cron — ClaudeClaw job`. Until that job is
configured, run the script manually before each new month.

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
