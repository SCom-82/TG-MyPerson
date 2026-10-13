"""copy / delta / verify of the account isolation migration (ТЗ 2026-10-13 §8.2–8.4).

Old shared tables (tg_*, max_*, chat_access, snapshots, users_registry) → the
per-account tables. Runs as the table owner (MIGRATIONS_DATABASE_URL, else
DATABASE_URL): RLS does not apply. No Telegram calls.

    python -m app.scripts.migrate_isolation copy   [--self work=7353998268] [--probe-report FILE] \
        [--split-date 2025-11-01 --split-id 20000 --low-account work --high-account personal-ro \
         --max-account max-work]
    python -m app.scripts.migrate_isolation delta  (same options)
    python -m app.scripts.migrate_isolation verify (same options)

Idempotent: every insert goes by the natural key of the new table
(ON CONFLICT … DO NOTHING; delta: DO UPDATE with the merge rules of §4.2).
Snapshots and raw events keep their legacy ids (their only key). One
transaction per step; messages and media in batches of 50k legacy ids.

Ownership (§8.2.C/D):
- MAX chats → --max-account.
- TG channels/supergroups → accounts from mig_membership (probe); without any
  member, signals: chat_access, an own message (from_user_id = the account's
  user), watch_chat_ids, audit_logs (alias + chat_id of reading tools).
- TG private chats and basic groups: every message to exactly one account —
  before --split-date the high account, then message_id < --split-id the low
  account, else the high one; preconditions checked first (stop otherwise).
- No owner → mig_unassigned (reported, not copied; D2).
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.scripts.account_partitions import DATA_TABLES, connect

BATCH = 50_000
READ_TOOLS = ("list_messages", "get_chat_detail", "download_media", "trigger_backfill", "backfill_history")
V3_CHATS = (-1002175364727, -1002930576682, -1003963089858, -1004467545855)
WATERMARK_TABLES = ("tg_messages", "tg_media", "max_messages", "max_media", "tg_chats", "tg_users")

E164_FN = r"""
CREATE OR REPLACE FUNCTION pg_temp.e164(p text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN d IS NULL OR length(d) < 10 THEN NULL
              WHEN length(d) = 11 AND left(d, 1) = '8' THEN '+7' || substr(d, 2)
              ELSE '+' || d END
  FROM (SELECT nullif(regexp_replace(coalesce(p, ''), '\D', '', 'g'), '') AS d) s
$$
"""

MIG_DDL = (
    """CREATE TABLE IF NOT EXISTS mig_watermark (
        tbl text PRIMARY KEY, max_id bigint, at timestamptz NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mig_chat_owner (
        platform text NOT NULL, legacy_chat_id bigint NOT NULL, account_id int NOT NULL,
        rule text NOT NULL, PRIMARY KEY (platform, legacy_chat_id, account_id))""",
    """CREATE TABLE IF NOT EXISTS mig_unassigned (
        legacy_chat_id bigint PRIMARY KEY, chat_type text, title text, messages bigint)""",
    """CREATE TABLE IF NOT EXISTS mig_membership (
        account_id int NOT NULL REFERENCES accounts(id), external_chat_id bigint NOT NULL,
        chat_type text NOT NULL, title text, role text, access_hash bigint,
        PRIMARY KEY (account_id, external_chat_id))""",
)


class PreconditionFailed(Exception):
    """§8.2.D: the attribution rule does not hold — stop, escalate (ТЗ §13)."""


@dataclass
class Options:
    split_date: date = date(2025, 11, 1)
    split_id: int = 20000
    low_account: str = "work"
    high_account: str = "personal-ro"
    max_account: str = "max-work"
    selves: dict[str, int] = field(default_factory=dict)  # alias → own user id
    probe_report: Path | None = None


@dataclass
class Ctx:
    conn: Any
    opts: Options
    low: int
    high: int
    max: int | None
    delta_wm: dict[str, tuple[int | None, datetime]] | None = None  # delta: table → (max_id, at)

    @property
    def params(self) -> dict:
        return {"split_date": self.opts.split_date, "split_id": self.opts.split_id,
                "low": self.low, "high": self.high, "max": self.max}

    def q(self, sql: str, params: dict | None = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, {**self.params, **(params or {})})
            return cur.rowcount

    def rows(self, sql: str, params: dict | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, {**self.params, **(params or {})})
            return cur.fetchall()

    def one(self, sql: str, params: dict | None = None):
        rows = self.rows(sql, params)
        return rows[0][0] if rows else None


# Owner of a TG private/basic-group message (§8.2.D), alias m = tg_messages.
OWNER_D = ("(CASE WHEN m.tg_date < %(split_date)s THEN %(high)s "
           "WHEN m.message_id < %(split_id)s THEN %(low)s ELSE %(high)s END)")
PRIVATE = "('private', 'group')"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _account_id(conn, alias: str, platform: str | None = None) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE alias = %s" + (" AND platform = %s" if platform else ""),
                    (alias, platform) if platform else (alias,))
        row = cur.fetchone()
    return row[0] if row else None


def open_ctx(opts: Options) -> Ctx:
    conn = connect()
    low = _account_id(conn, opts.low_account, "telegram")
    high = _account_id(conn, opts.high_account, "telegram")
    if low is None or high is None:
        raise SystemExit(f"TG accounts {opts.low_account!r}/{opts.high_account!r} not found")
    ctx = Ctx(conn, opts, low, high, _account_id(conn, opts.max_account, "max"))
    with conn.cursor() as cur:
        cur.execute(E164_FN)
        for ddl in MIG_DDL:
            cur.execute(ddl)
    conn.commit()
    return ctx


def check_partitions(ctx: Ctx) -> None:
    missing = ctx.rows("SELECT a.alias, p.parent FROM accounts a, account_partitions(a.id) p WHERE p.rows IS NULL")
    if missing:
        raise SystemExit(f"partitions missing (run create_account_partitions): {missing}")


def _self_ids_from_report(path: Path | None) -> dict[str, int]:
    if path is None:
        candidates = sorted(Path("/tmp").glob("migrate_isolation-probe-*.json"))
        path = candidates[-1] if candidates else None
    if path is None or not path.exists():
        return {}
    report = json.loads(path.read_text())
    return {
        alias: a["external_user_id"] for alias, a in report.get("accounts", {}).items()
        if a.get("status") == "ok" and a.get("external_user_id")
    }


# ---------------------------------------------------------------------------
# Steps (each returns a small dict for the report)
# ---------------------------------------------------------------------------

def step_watermark(ctx: Ctx) -> dict:
    out = {}
    for t in WATERMARK_TABLES:
        max_id = ctx.one(f"SELECT max(id) FROM {t}") if t not in ("tg_chats", "tg_users") else None
        ctx.q("INSERT INTO mig_watermark (tbl, max_id, at) VALUES (%(t)s, %(m)s, now())"
              " ON CONFLICT (tbl) DO UPDATE SET max_id = excluded.max_id, at = excluded.at",
              {"t": t, "m": max_id})
        out[t] = max_id
    return out


def step_a_accounts(ctx: Ctx) -> dict:
    ctx.q("UPDATE accounts SET external_user_id = tg_user_id WHERE platform = 'telegram'"
          " AND external_user_id IS NULL AND tg_user_id IS NOT NULL")
    ctx.q("UPDATE accounts SET external_user_id = platform_user_id WHERE platform = 'max'"
          " AND external_user_id IS NULL AND platform_user_id IS NOT NULL")
    given = {**_self_ids_from_report(ctx.opts.probe_report), **ctx.opts.selves}
    for alias, uid in given.items():
        current = ctx.rows("SELECT external_user_id FROM accounts WHERE alias = %(a)s", {"a": alias})
        if not current:
            continue
        if current[0][0] is not None and current[0][0] != uid:
            raise PreconditionFailed(f"{alias}: external_user_id {current[0][0]} != given {uid}")
        ctx.q("UPDATE accounts SET external_user_id = %(u)s WHERE alias = %(a)s", {"u": uid, "a": alias})
    missing = [r[0] for r in ctx.rows(
        "SELECT alias FROM accounts WHERE is_enabled AND external_user_id IS NULL ORDER BY id")]
    if missing:
        raise PreconditionFailed(f"external_user_id unknown for {missing}: pass --self alias=id or a probe report")
    return {r[0]: r[1] for r in ctx.rows("SELECT alias, external_user_id FROM accounts ORDER BY id")}


def step_b_identities(ctx: Ctx) -> dict:
    ctx.q("DROP TABLE IF EXISTS mig_src_ident")
    ctx.q("""
        CREATE TEMP TABLE mig_src_ident AS
        WITH ids AS (
            SELECT 'telegram' AS platform, id AS ext FROM tg_users
            UNION SELECT 'telegram', tg_user_id FROM users_registry WHERE tg_user_id IS NOT NULL
            UNION SELECT 'telegram', from_user_id FROM tg_messages WHERE from_user_id IS NOT NULL
            UNION SELECT 'telegram', tg_user_id FROM chat_member_records WHERE tg_user_id IS NOT NULL
            UNION SELECT 'max', id FROM max_users
            UNION SELECT 'max', from_user_id FROM max_messages WHERE from_user_id IS NOT NULL
            UNION SELECT platform, external_user_id FROM accounts WHERE external_user_id IS NOT NULL
        )
        SELECT DISTINCT ON (ids.platform, ids.ext)
            ids.platform, ids.ext,
            coalesce(nullif(u.username, ''), nullif(mu.username, ''), nullif(r.primary_username, '')) AS username,
            coalesce(nullif(u.first_name, ''), nullif(mu.first_name, '')) AS first_name,
            coalesce(nullif(u.last_name, ''), nullif(mu.last_name, '')) AS last_name,
            pg_temp.e164(coalesce(nullif(u.phone, ''), nullif(mu.phone, ''), nullif(r.primary_phone, ''))) AS phone,
            coalesce(u.is_bot, mu.is_bot, false) AS is_bot,
            coalesce(r.tags, '{}') AS tags,
            coalesce(nullif(trim(concat_ws(' ', coalesce(u.first_name, mu.first_name),
                                                coalesce(u.last_name, mu.last_name))), ''),
                     nullif(r.primary_name, ''), coalesce(u.username, mu.username, r.primary_username)) AS display,
            coalesce(u.updated_at, mu.updated_at) AS updated_at,
            NULL::bigint AS person_id
        FROM ids
        LEFT JOIN tg_users u ON ids.platform = 'telegram' AND u.id = ids.ext
        LEFT JOIN max_users mu ON ids.platform = 'max' AND mu.id = ids.ext
        LEFT JOIN users_registry r ON ids.platform = 'telegram' AND r.tg_user_id = ids.ext
        ORDER BY ids.platform, ids.ext, r.id
    """)
    updated = 0
    if ctx.delta_wm is not None:
        # Existing identities: refresh from rows changed since the watermark (non-empty wins).
        updated = ctx.q("""
            UPDATE identities i SET
              username = coalesce(s.username, i.username), first_name = coalesce(s.first_name, i.first_name),
              last_name = coalesce(s.last_name, i.last_name), phone = coalesce(s.phone, i.phone),
              is_bot = s.is_bot
            FROM mig_src_ident s
            WHERE i.platform = s.platform AND i.external_user_id = s.ext AND s.updated_at > %(at)s
        """, {"at": ctx.delta_wm["tg_users"][1]})
    ctx.q("DELETE FROM mig_src_ident s USING identities i"
          " WHERE i.platform = s.platform AND i.external_user_id = s.ext")
    ctx.q("UPDATE mig_src_ident SET person_id = nextval(pg_get_serial_sequence('people', 'id'))")
    people = ctx.q("""
        INSERT INTO people (id, display_name, primary_phone, tags) OVERRIDING SYSTEM VALUE
        SELECT person_id, display, phone, tags FROM mig_src_ident
    """)
    ctx.q("""
        INSERT INTO identities (person_id, platform, external_user_id, username, first_name, last_name, phone, is_bot)
        SELECT person_id, platform, ext, username, first_name, last_name, phone, is_bot FROM mig_src_ident
        ON CONFLICT (platform, external_user_id) DO NOTHING
    """)
    merged = _auto_merge(ctx)
    return {"new_people": people, "updated_identities": updated, "auto_merged": merged}


def _auto_merge(ctx: Ctx) -> int:
    """§7.1 p.4 over the whole base: same E.164 phone TG↔MAX, different person, nobody pinned."""
    merged = 0
    pairs = ctx.rows("""
        SELECT t.id, t.person_id, m.id, m.person_id FROM identities t
        JOIN identities m ON m.phone = t.phone AND m.platform = 'max' AND t.platform = 'telegram'
        WHERE t.person_id <> m.person_id AND NOT t.person_pinned AND NOT m.person_pinned
        ORDER BY t.id
    """)
    for t_id, t_person, m_id, m_person in pairs:
        cur = dict(ctx.rows("SELECT id, person_id FROM identities WHERE id IN (%(a)s, %(b)s)",
                            {"a": t_id, "b": m_id}))
        if cur[t_id] == cur[m_id]:
            continue
        target, source = sorted((cur[t_id], cur[m_id]))
        moved = t_id if cur[t_id] == source else m_id
        ctx.q("UPDATE identities SET person_id = %(t)s WHERE id = %(i)s", {"t": target, "i": moved})
        ctx.q("""
            UPDATE people t SET tags = ARRAY(SELECT DISTINCT unnest(t.tags || s.tags) ORDER BY 1),
              notes = CASE WHEN coalesce(s.notes, '') = '' THEN t.notes
                           WHEN coalesce(t.notes, '') = '' THEN s.notes ELSE t.notes || E'\\n' || s.notes END,
              display_name = coalesce(t.display_name, s.display_name),
              primary_phone = coalesce(t.primary_phone, s.primary_phone)
            FROM people s WHERE t.id = %(t)s AND s.id = %(s)s
        """, {"t": target, "s": source})
        ctx.q("INSERT INTO people_merge_log (action, from_person_id, to_person_id, identity_id, reason)"
              " VALUES ('auto_merge', %(s)s, %(t)s, %(i)s, 'phone (migrate_isolation)')",
              {"s": source, "t": target, "i": moved})
        ctx.q("DELETE FROM people p WHERE p.id = %(s)s AND NOT EXISTS"
              " (SELECT 1 FROM identities i WHERE i.person_id = p.id)", {"s": source})
        merged += 1
    return merged


def step_c_owners(ctx: Ctx) -> dict:
    q = ctx.q
    if ctx.max is not None:
        q("INSERT INTO mig_chat_owner SELECT 'max', id, %(max)s, 'max' FROM max_chats ON CONFLICT DO NOTHING")
    q(f"""INSERT INTO mig_chat_owner
          SELECT 'telegram', c.id, mm.account_id, 'member' FROM tg_chats c
          JOIN mig_membership mm ON mm.external_chat_id = c.id
          ON CONFLICT DO NOTHING""")
    no_member = (f"(SELECT c.id FROM tg_chats c WHERE c.chat_type NOT IN {PRIVATE} AND NOT EXISTS"
                 " (SELECT 1 FROM mig_membership mm WHERE mm.external_chat_id = c.id))")
    q(f"""INSERT INTO mig_chat_owner
          SELECT 'telegram', ca.chat_id, ca.account_id, 'signal:chat_access' FROM chat_access ca
          WHERE ca.chat_id IN {no_member} ON CONFLICT DO NOTHING""")
    q(f"""INSERT INTO mig_chat_owner
          SELECT DISTINCT 'telegram', m.chat_id, a.id, 'signal:own_message' FROM tg_messages m
          JOIN accounts a ON a.platform = 'telegram' AND a.external_user_id = m.from_user_id
          WHERE m.chat_id IN {no_member} ON CONFLICT DO NOTHING""")
    q(f"""INSERT INTO mig_chat_owner
          SELECT 'telegram', c.id, a.id, 'signal:watch' FROM {no_member} c
          JOIN accounts a ON a.platform = 'telegram' AND c.id = ANY(a.watch_chat_ids)
          ON CONFLICT DO NOTHING""")
    q(f"""INSERT INTO mig_chat_owner
          SELECT DISTINCT 'telegram', l.chat_id, a.id, 'signal:audit' FROM audit_logs l
          JOIN accounts a ON a.platform = 'telegram' AND a.alias = l.alias
          WHERE l.tool IN %(tools)s AND l.chat_id IN {no_member} ON CONFLICT DO NOTHING""",
      {"tools": READ_TOOLS})
    q(f"""INSERT INTO mig_chat_owner
          SELECT DISTINCT 'telegram', m.chat_id, {OWNER_D}, 'rule_d' FROM tg_messages m
          JOIN tg_chats c ON c.id = m.chat_id WHERE c.chat_type IN {PRIVATE}
          ON CONFLICT DO NOTHING""")
    q("DELETE FROM mig_unassigned u WHERE EXISTS (SELECT 1 FROM mig_chat_owner o"
      " WHERE o.platform = 'telegram' AND o.legacy_chat_id = u.legacy_chat_id)")
    q("""INSERT INTO mig_unassigned
         SELECT c.id, c.chat_type, c.title, (SELECT count(*) FROM tg_messages m WHERE m.chat_id = c.id)
         FROM tg_chats c WHERE NOT EXISTS (SELECT 1 FROM mig_chat_owner o
           WHERE o.platform = 'telegram' AND o.legacy_chat_id = c.id)
         ON CONFLICT (legacy_chat_id) DO UPDATE SET messages = excluded.messages""")
    by_rule = dict(ctx.rows("SELECT rule, count(*) FROM mig_chat_owner GROUP BY rule ORDER BY rule"))
    return {"owners_by_rule": by_rule,
            "unassigned_chats": ctx.one("SELECT count(*) FROM mig_unassigned"),
            "unassigned_messages": ctx.one("SELECT coalesce(sum(messages), 0) FROM mig_unassigned")}


def step_d_preconditions(ctx: Ctx) -> dict:
    others = [r[0] for r in ctx.rows(
        "SELECT alias FROM accounts WHERE platform = 'telegram' AND id NOT IN (%(low)s, %(high)s)")]
    if others:
        raise PreconditionFailed(f"rule D assumes two TG accounts; also found {others}")
    mismatch = ctx.one(f"""
        SELECT count(*) FROM tg_messages m JOIN tg_chats c ON c.id = m.chat_id
        JOIN accounts a ON a.platform = 'telegram' AND a.external_user_id = m.from_user_id
        WHERE c.chat_type IN {PRIVATE} AND m.raw_data->>'out' = 'true' AND a.id <> {OWNER_D}
    """)
    reference = ctx.one(f"""
        SELECT count(*) FROM tg_messages m JOIN tg_chats c ON c.id = m.chat_id
        JOIN accounts a ON a.platform = 'telegram' AND a.external_user_id = m.from_user_id
        WHERE c.chat_type IN {PRIVATE} AND m.raw_data->>'out' = 'true'
    """)
    if mismatch:
        raise PreconditionFailed(f"reference 'out' messages contradict rule D: {mismatch} of {reference}")
    gap = ctx.one(f"""
        SELECT count(*) FROM tg_messages m JOIN tg_chats c ON c.id = m.chat_id
        WHERE c.chat_type IN {PRIVATE} AND m.tg_date >= %(split_date)s
          AND m.message_id >= %(split_id)s AND m.message_id < 150000
    """)
    if gap:
        raise PreconditionFailed(f"{gap} messages after split-date with message_id in [split-id, 150000)")
    return {"reference_out": reference, "mismatch": 0, "gap": 0}


def _chat_conflict(delta: bool) -> str:
    if not delta:
        return "ON CONFLICT (account_id, external_id) DO NOTHING"
    return """ON CONFLICT (account_id, external_id) DO UPDATE SET
        chat_type = excluded.chat_type, title = excluded.title,
        username = coalesce(excluded.username, chats.username),
        description = coalesce(excluded.description, chats.description),
        members_count = coalesce(excluded.members_count, chats.members_count),
        last_message_external_id = greatest(chats.last_message_external_id, excluded.last_message_external_id),
        last_message_at = greatest(chats.last_message_at, excluded.last_message_at),
        raw = coalesce(excluded.raw, chats.raw)"""


def step_e_chats(ctx: Ctx) -> dict:
    delta = ctx.delta_wm is not None
    where_tg = " AND c.updated_at > %(at)s" if delta else ""
    params = {"at": ctx.delta_wm["tg_chats"][1]} if delta else {}
    tg = ctx.q(f"""
        INSERT INTO chats (account_id, external_id, chat_type, title, username, description, members_count,
                           access_hash, my_role, is_monitored, last_message_external_id, last_message_at, raw,
                           first_seen_at, created_at, updated_at)
        SELECT o.account_id, c.id, c.chat_type, c.title, c.username, c.description, c.members_count,
               mm.access_hash,
               coalesce(mm.role, CASE WHEN ca.role IN ('member', 'admin', 'creator') THEN ca.role END),
               coalesce(c.is_monitored, true), c.last_message_id, c.last_message_at, c.raw_data,
               coalesce(ca.first_seen_at, c.created_at, now()), coalesce(c.created_at, now()),
               coalesce(c.updated_at, now())
        FROM mig_chat_owner o JOIN tg_chats c ON o.platform = 'telegram' AND c.id = o.legacy_chat_id
        LEFT JOIN mig_membership mm ON mm.account_id = o.account_id AND mm.external_chat_id = c.id
        LEFT JOIN chat_access ca ON ca.account_id = o.account_id AND ca.chat_id = c.id
        WHERE true {where_tg}
        {_chat_conflict(delta)}
    """, params)
    mx = ctx.q(f"""
        INSERT INTO chats (account_id, external_id, chat_type, title, username, description, members_count,
                           is_monitored, last_message_external_id, last_message_at, raw,
                           first_seen_at, created_at, updated_at)
        SELECT o.account_id, c.id, c.chat_type, c.title, c.username, c.description, c.members_count,
               coalesce(c.is_monitored, true), c.last_message_id, c.last_message_at, c.raw_data,
               coalesce(c.created_at, now()), coalesce(c.created_at, now()), coalesce(c.updated_at, now())
        FROM mig_chat_owner o JOIN max_chats c ON o.platform = 'max' AND c.id = o.legacy_chat_id
        WHERE true {where_tg}
        {_chat_conflict(delta)}
    """, params)
    return {"tg_chats": tg, "max_chats": mx}


def step_f_users(ctx: Ctx) -> dict:
    ctx.q("DROP TABLE IF EXISTS mig_acc_users")
    ctx.q(f"""
        CREATE TEMP TABLE mig_acc_users AS
        SELECT o.account_id, m.from_user_id AS ext, 1 AS prio, 'message' AS via
          FROM tg_messages m JOIN tg_chats c ON c.id = m.chat_id
          JOIN mig_chat_owner o ON o.platform = 'telegram' AND o.legacy_chat_id = m.chat_id
          WHERE m.from_user_id IS NOT NULL AND (c.chat_type NOT IN {PRIVATE} OR o.account_id = {OWNER_D})
        UNION SELECT s.account_id, r.tg_user_id, 2, 'member'
          FROM chat_member_records r JOIN chat_members_snapshots s ON s.id = r.snapshot_id
          WHERE r.tg_user_id IS NOT NULL AND s.account_id IS NOT NULL
        UNION SELECT coalesce(src.account_id, %(high)s), reg.tg_user_id, 3, 'resolve'
          FROM users_registry reg LEFT JOIN users_registry_sources src ON src.registry_id = reg.id
          WHERE reg.tg_user_id IS NOT NULL
        UNION SELECT o.account_id, c.id, 4, 'dialog'
          FROM mig_chat_owner o JOIN tg_chats c ON o.platform = 'telegram' AND c.id = o.legacy_chat_id
          WHERE c.chat_type = 'private'
        UNION SELECT a.id, a.external_user_id, 4, 'dialog' FROM accounts a
          WHERE a.platform = 'telegram' AND a.external_user_id IS NOT NULL
        UNION SELECT %(max)s, mu.id, 1, 'message' FROM max_users mu WHERE %(max)s IS NOT NULL
        UNION SELECT %(max)s, mm.from_user_id, 1, 'message' FROM max_messages mm
          WHERE %(max)s IS NOT NULL AND mm.from_user_id IS NOT NULL
        UNION SELECT a.id, a.external_user_id, 4, 'dialog' FROM accounts a
          WHERE a.platform = 'max' AND a.external_user_id IS NOT NULL
    """)
    inserted = ctx.q("""
        INSERT INTO users (account_id, external_id, identity_id, username, first_name, last_name, phone,
                           is_bot, is_self, last_seen_via, raw, first_seen_at, updated_at)
        SELECT DISTINCT ON (x.account_id, x.ext)
               x.account_id, x.ext, i.id,
               coalesce(u.username, mu.username, i.username), coalesce(u.first_name, mu.first_name, i.first_name),
               coalesce(u.last_name, mu.last_name, i.last_name), pg_temp.e164(coalesce(u.phone, mu.phone, i.phone)),
               coalesce(u.is_bot, mu.is_bot, i.is_bot), x.ext = a.external_user_id, x.via,
               coalesce(u.raw_data, mu.raw_data),
               coalesce(u.first_seen_at, mu.first_seen_at, now()), coalesce(u.updated_at, mu.updated_at, now())
        FROM mig_acc_users x
        JOIN accounts a ON a.id = x.account_id
        JOIN identities i ON i.platform = a.platform AND i.external_user_id = x.ext
        LEFT JOIN tg_users u ON a.platform = 'telegram' AND u.id = x.ext
        LEFT JOIN max_users mu ON a.platform = 'max' AND mu.id = x.ext
        ORDER BY x.account_id, x.ext, x.prio
        ON CONFLICT (account_id, external_id) DO NOTHING
    """)
    return {"users": inserted}


def _message_conflict(delta: bool) -> str:
    if not delta:
        return "ON CONFLICT (account_id, chat_id, external_id) DO NOTHING"
    return """ON CONFLICT (account_id, chat_id, external_id) DO UPDATE SET
        text = excluded.text, text_html = excluded.text_html, is_edited = excluded.is_edited,
        edited_at = excluded.edited_at, views = coalesce(excluded.views, messages.views),
        raw = excluded.raw, deleted_at = coalesce(messages.deleted_at, excluded.deleted_at),
        sender_user_id = coalesce(excluded.sender_user_id, messages.sender_user_id)
        WHERE NOT messages.is_edited OR excluded.is_edited OR excluded.deleted_at IS NOT NULL"""


def _batches(ctx: Ctx, table: str, sql: str, extra: dict | None = None) -> int:
    lo_hi = ctx.rows(f"SELECT min(id), max(id) FROM {table}")[0]
    if lo_hi[0] is None:
        return 0
    if ctx.delta_wm is not None:
        # delta: the watermark filter selects few rows — one pass, not a walk over all batches.
        n = ctx.q(sql, {"lo": lo_hi[0] - 1, "hi": lo_hi[1], **(extra or {})})
        ctx.conn.commit()
        return n
    total = 0
    lo = lo_hi[0] - 1
    while lo < lo_hi[1]:
        hi = lo + BATCH
        total += ctx.q(sql, {"lo": lo, "hi": hi, **(extra or {})})
        ctx.conn.commit()
        lo = hi
    return total


def step_g_messages(ctx: Ctx) -> dict:
    delta = ctx.delta_wm is not None
    tg_filter = mx_filter = ""
    extra: dict = {}
    if delta:
        wm_id, wm_at = ctx.delta_wm["tg_messages"]
        tg_filter = " AND (m.id > %(wm_id)s OR m.edit_date > %(wm_at)s)"
        mx_id, mx_at = ctx.delta_wm["max_messages"]
        mx_filter = " AND (m.id > %(mx_id)s OR m.edit_date > %(mx_at)s OR m.deleted_at > %(mx_at)s)"
        extra = {"wm_id": wm_id or 0, "wm_at": wm_at, "mx_id": mx_id or 0, "mx_at": mx_at}
    tg = _batches(ctx, "tg_messages", f"""
        INSERT INTO messages (account_id, chat_id, external_id, sender_user_id, sender_chat_external_id,
                              reply_to_external_id, forward_from_chat_external_id, forward_from_message_external_id,
                              message_type, text, text_html, sent_at, is_outgoing, is_edited, edited_at, views,
                              raw, created_at)
        SELECT o.account_id, ch.id, m.message_id, us.id, m.sender_chat_id, m.reply_to_message_id,
               m.forward_from_chat_id, m.forward_from_message_id, coalesce(m.message_type, 'text'), m.text,
               m.text_html, m.tg_date, coalesce(m.from_user_id = a.external_user_id, false),
               coalesce(m.is_edited, false), m.edit_date, m.views, m.raw_data, coalesce(m.created_at, now())
        FROM tg_messages m
        JOIN tg_chats c ON c.id = m.chat_id
        JOIN mig_chat_owner o ON o.platform = 'telegram' AND o.legacy_chat_id = m.chat_id
        JOIN accounts a ON a.id = o.account_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = m.chat_id
        LEFT JOIN users us ON us.account_id = o.account_id AND us.external_id = m.from_user_id
        WHERE m.id > %(lo)s AND m.id <= %(hi)s
          AND (c.chat_type NOT IN {PRIVATE} OR o.account_id = {OWNER_D}) {tg_filter}
        {_message_conflict(delta)}
    """, extra)
    mx = _batches(ctx, "max_messages", f"""
        INSERT INTO messages (account_id, chat_id, external_id, sender_user_id, sender_chat_external_id,
                              reply_to_external_id, forward_from_chat_external_id, forward_from_message_external_id,
                              message_type, text, text_html, sent_at, is_outgoing, is_edited, edited_at, views,
                              raw, deleted_at, created_at)
        SELECT o.account_id, ch.id, m.message_id, us.id, m.sender_chat_id, m.reply_to_message_id,
               m.forward_from_chat_id, m.forward_from_message_id, coalesce(m.message_type, 'text'), m.text,
               m.text_html, m.tg_date, coalesce(m.from_user_id = a.external_user_id, false),
               coalesce(m.is_edited, false), m.edit_date, m.views, m.raw_data, m.deleted_at,
               coalesce(m.created_at, now())
        FROM max_messages m
        JOIN mig_chat_owner o ON o.platform = 'max' AND o.legacy_chat_id = m.chat_id
        JOIN accounts a ON a.id = o.account_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = m.chat_id
        LEFT JOIN users us ON us.account_id = o.account_id AND us.external_id = m.from_user_id
        WHERE m.id > %(lo)s AND m.id <= %(hi)s {mx_filter}
        {_message_conflict(delta)}
    """, extra)
    return {"tg_messages": tg, "max_messages": mx}


def step_h_media(ctx: Ctx) -> dict:
    delta = ctx.delta_wm is not None
    tg_f = " AND md.id > %(wm_id)s" if delta else ""
    mx_f = " AND md.id > %(mx_id)s" if delta else ""
    extra = ({"wm_id": ctx.delta_wm["tg_media"][0] or 0, "mx_id": ctx.delta_wm["max_media"][0] or 0}
             if delta else {})
    tg = _batches(ctx, "tg_media", f"""
        INSERT INTO media (account_id, message_id, attach_index, file_external_id, file_unique_id, file_type,
                           file_name, file_size, mime_type, storage_path, created_at)
        SELECT nm.account_id, nm.id, 0, md.file_id, md.file_unique_id, md.file_type, md.file_name,
               md.file_size, md.mime_type, md.local_path, coalesce(md.created_at, now())
        FROM tg_media md JOIN tg_messages m ON m.id = md.message_pk
        JOIN mig_chat_owner o ON o.platform = 'telegram' AND o.legacy_chat_id = m.chat_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = m.chat_id
        JOIN messages nm ON nm.account_id = o.account_id AND nm.chat_id = ch.id AND nm.external_id = m.message_id
        WHERE md.id > %(lo)s AND md.id <= %(hi)s {tg_f}
        ON CONFLICT (account_id, message_id, attach_index) DO NOTHING
    """, extra)
    mx = _batches(ctx, "max_media", f"""
        INSERT INTO media (account_id, message_id, attach_index, file_external_id, file_unique_id, file_type,
                           file_name, file_size, mime_type, storage_path, created_at)
        SELECT nm.account_id, nm.id, coalesce(md.attach_index, 0), md.file_id, md.file_unique_id, md.file_type,
               md.file_name, md.file_size, md.mime_type, md.local_path, coalesce(md.created_at, now())
        FROM max_media md JOIN max_messages m ON m.id = md.message_pk
        JOIN mig_chat_owner o ON o.platform = 'max' AND o.legacy_chat_id = m.chat_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = m.chat_id
        JOIN messages nm ON nm.account_id = o.account_id AND nm.chat_id = ch.id AND nm.external_id = m.message_id
        WHERE md.id > %(lo)s AND md.id <= %(hi)s {mx_f}
        ON CONFLICT (account_id, message_id, attach_index) DO NOTHING
    """, extra)
    return {"tg_media": tg, "max_media": mx}


def step_i_sync_state(ctx: Ctx) -> dict:
    delta = ctx.delta_wm is not None
    conflict = ("ON CONFLICT (account_id, chat_id) DO UPDATE SET oldest_external_id = excluded.oldest_external_id,"
                " newest_external_id = excluded.newest_external_id, oldest_ts = excluded.oldest_ts,"
                " newest_ts = excluded.newest_ts, is_fully_synced = excluded.is_fully_synced,"
                " total_synced = excluded.total_synced, last_backfill_at = excluded.last_backfill_at,"
                " last_catchup_at = excluded.last_catchup_at") if delta else \
        "ON CONFLICT (account_id, chat_id) DO NOTHING"
    since = " AND s.updated_at > %(at)s" if delta else ""
    params = {"at": ctx.delta_wm["tg_chats"][1]} if delta else {}
    copied = ctx.q(f"""
        INSERT INTO sync_state (account_id, chat_id, oldest_external_id, newest_external_id, is_fully_synced,
                                total_synced, last_backfill_at)
        SELECT o.account_id, ch.id, s.oldest_message_id, s.newest_message_id, coalesce(s.is_fully_synced, false),
               coalesce(s.total_messages_synced, 0), s.last_backfill_at
        FROM tg_sync_state s JOIN tg_chats c ON c.id = s.chat_id
        JOIN mig_chat_owner o ON o.platform = 'telegram' AND o.legacy_chat_id = s.chat_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = s.chat_id
        WHERE (c.chat_type NOT IN {PRIVATE}
               OR (SELECT count(*) FROM mig_chat_owner x WHERE x.platform = 'telegram'
                   AND x.legacy_chat_id = s.chat_id) = 1) {since}
        {conflict}
    """, params)
    recomputed = ctx.q(f"""
        INSERT INTO sync_state (account_id, chat_id, oldest_external_id, newest_external_id, is_fully_synced,
                                total_synced, last_backfill_at)
        SELECT ch.account_id, ch.id, min(nm.external_id), max(nm.external_id), false, count(*), s.last_backfill_at
        FROM tg_sync_state s JOIN tg_chats c ON c.id = s.chat_id AND c.chat_type IN {PRIVATE}
        JOIN chats ch ON ch.external_id = s.chat_id
        JOIN accounts a ON a.id = ch.account_id AND a.platform = 'telegram'
        JOIN messages nm ON nm.account_id = ch.account_id AND nm.chat_id = ch.id
        WHERE (SELECT count(*) FROM mig_chat_owner x WHERE x.platform = 'telegram'
               AND x.legacy_chat_id = s.chat_id) > 1 {since}
        GROUP BY ch.account_id, ch.id, s.last_backfill_at
        {conflict}
    """, params)
    mx = ctx.q(f"""
        INSERT INTO sync_state (account_id, chat_id, oldest_external_id, newest_external_id, oldest_ts, newest_ts,
                                is_fully_synced, total_synced, last_backfill_at, last_catchup_at)
        SELECT o.account_id, ch.id, s.oldest_message_id, s.newest_message_id,
               to_timestamp(s.oldest_time_ms / 1000.0), to_timestamp(s.newest_time_ms / 1000.0),
               coalesce(s.is_fully_synced, false), coalesce(s.total_messages_synced, 0),
               s.last_backfill_at, s.last_catchup_at
        FROM max_sync_state s JOIN mig_chat_owner o ON o.platform = 'max' AND o.legacy_chat_id = s.chat_id
        JOIN chats ch ON ch.account_id = o.account_id AND ch.external_id = s.chat_id
        WHERE true {since}
        {conflict}
    """, params)
    return {"copied": copied, "recomputed": recomputed, "max": mx}


def step_j_snapshots(ctx: Ctx) -> dict:
    snaps = ctx.q("""
        INSERT INTO member_snapshots (account_id, id, chat_id, taken_at, members_count, source, note)
        SELECT s.account_id, s.id, ch.id, s.taken_at, s.members_count, s.source, s.note
        FROM chat_members_snapshots s JOIN chats ch ON ch.account_id = s.account_id AND ch.external_id = s.chat_id
        ON CONFLICT (account_id, id) DO NOTHING
    """)
    skipped = ctx.one("""
        SELECT count(*) FROM chat_members_snapshots s WHERE NOT EXISTS
          (SELECT 1 FROM chats ch WHERE ch.account_id = s.account_id AND ch.external_id = s.chat_id)
    """)
    members = ctx.q("""
        INSERT INTO snapshot_members (account_id, id, snapshot_id, user_id, external_user_id, username,
                                      first_name, last_name, phone, role, raw)
        SELECT s.account_id, r.id, r.snapshot_id, us.id, r.tg_user_id, r.username, r.first_name, r.last_name,
               r.phone, r.role, r.raw
        FROM chat_member_records r JOIN member_snapshots s ON s.id = r.snapshot_id
        LEFT JOIN users us ON us.account_id = s.account_id AND us.external_id = r.tg_user_id
        ON CONFLICT (account_id, id) DO NOTHING
    """)
    for seq, table in (("member_snapshots_id_seq", "member_snapshots"),
                       ("snapshot_members_id_seq", "snapshot_members")):
        ctx.rows(f"SELECT setval('{seq}', greatest((SELECT coalesce(max(id), 0) FROM {table}) + 1,"
                 f" (SELECT last_value FROM {seq})), false)")
    return {"snapshots": snaps, "snapshot_members": members, "skipped_snapshots_chat_not_owned": skipped}


def step_k_raw_events(ctx: Ctx) -> dict:
    if ctx.max is None:
        return {"raw_events": 0}
    n = ctx.q("""
        INSERT INTO raw_events (account_id, id, kind, received_at, payload, normalized, error)
        SELECT coalesce(e.account_id, %(max)s), e.id, 'max:' || coalesce(e.opcode::text, '?'),
               e.received_at, e.payload, coalesce(e.normalized, false), e.error
        FROM max_raw_events e
        ON CONFLICT (account_id, id) DO NOTHING
    """)
    ctx.rows("SELECT setval('raw_events_id_seq', greatest((SELECT coalesce(max(id), 0) FROM raw_events) + 1,"
             " (SELECT last_value FROM raw_events_id_seq)), false)")
    return {"raw_events": n}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# Fresh bulk inserts have no statistics yet: without ANALYZE the planner starts the
# next step's joins from "empty" new tables (step H took minutes instead of seconds).
ANALYZE_AFTER = {
    "C_owners": "mig_chat_owner, mig_membership",
    "E_chats": "chats",
    "F_users": "users, identities, people",
    "G_messages": "messages",
    "H_media": "media",
}


def _run_steps(ctx: Ctx, steps: list[tuple[str, Callable[[Ctx], dict]]]) -> dict:
    report: dict[str, Any] = {"steps": {}}
    for name, fn in steps:
        t0 = time.monotonic()
        try:
            result = fn(ctx)
            ctx.conn.commit()
            if name in ANALYZE_AFTER:
                ctx.q(f"ANALYZE {ANALYZE_AFTER[name]}")
                ctx.conn.commit()
        except Exception:
            ctx.conn.rollback()
            raise
        report["steps"][name] = {**result, "seconds": round(time.monotonic() - t0, 2)}
    return report


def _counts(ctx: Ctx) -> dict:
    return {t: ctx.one(f"SELECT count(*) FROM {t}") for t in (*DATA_TABLES, "people", "identities")}


COPY_STEPS = [
    ("A_accounts", step_a_accounts),
    ("B_identities", step_b_identities),
    ("C_owners", step_c_owners),
    ("D_preconditions", step_d_preconditions),
    ("E_chats", step_e_chats),
    ("F_users", step_f_users),
    ("G_messages", step_g_messages),
    ("H_media", step_h_media),
    ("I_sync_state", step_i_sync_state),
    ("J_snapshots", step_j_snapshots),
    ("K_raw_events", step_k_raw_events),
]


def copy(opts: Options) -> dict:
    ctx = open_ctx(opts)
    try:
        check_partitions(ctx)
        before = _counts(ctx)
        step_watermark(ctx)
        ctx.conn.commit()
        report = _run_steps(ctx, COPY_STEPS)
        after = _counts(ctx)
        report.update(command="copy", ok=True,
                      changes={t: after[t] - before[t] for t in after}, totals=after)
        return report
    finally:
        ctx.conn.close()


def delta(opts: Options) -> dict:
    ctx = open_ctx(opts)
    try:
        check_partitions(ctx)
        wm = {r[0]: (r[1], r[2]) for r in ctx.rows("SELECT tbl, max_id, at FROM mig_watermark")}
        if set(WATERMARK_TABLES) - set(wm):
            raise SystemExit("no watermark: run copy first")
        ctx.delta_wm = wm
        before = _counts(ctx)
        report = _run_steps(ctx, [s for s in COPY_STEPS if s[0] != "D_preconditions"])
        step_watermark(ctx)
        ctx.conn.commit()
        after = _counts(ctx)
        report.update(command="delta", ok=True, since={t: wm[t][1].isoformat() for t in wm},
                      changes={t: after[t] - before[t] for t in after})
        return report
    finally:
        ctx.conn.close()


# ---------------------------------------------------------------------------
# verify (§8.4)
# ---------------------------------------------------------------------------

def verify(opts: Options) -> dict:
    ctx = open_ctx(opts)
    checks: dict[str, dict] = {}
    try:
        checks["V1_copies"] = _v1(ctx)
        checks["V2_reference_out"] = _v2(ctx)
        checks["V3_loader_parity"] = _v3(ctx)
        checks["V4_one_self"] = _v4(ctx)
        checks["V5_external_user_id"] = _v5(ctx)
        checks["V6_sample"] = _v6(ctx)
        checks["V7_identities"] = _v7(ctx)
    finally:
        ctx.conn.close()
    return {"command": "verify", "ok": all(c["ok"] for c in checks.values()), "checks": checks}


def _v1(ctx: Ctx) -> dict:
    bad_tg = ctx.one(f"""
        WITH want AS (
            SELECT m.chat_id, m.message_id,
                   CASE WHEN c.chat_type IN {PRIVATE} THEN 1
                        ELSE (SELECT count(*) FROM mig_chat_owner o WHERE o.platform = 'telegram'
                              AND o.legacy_chat_id = m.chat_id) END AS n
            FROM tg_messages m JOIN tg_chats c ON c.id = m.chat_id
            WHERE NOT EXISTS (SELECT 1 FROM mig_unassigned u WHERE u.legacy_chat_id = m.chat_id)
        ), got AS (
            SELECT ch.external_id AS chat_id, nm.external_id AS message_id, count(*) AS n
            FROM messages nm JOIN chats ch ON ch.account_id = nm.account_id AND ch.id = nm.chat_id
            JOIN accounts a ON a.id = nm.account_id AND a.platform = 'telegram'
            GROUP BY 1, 2
        )
        SELECT count(*) FROM want LEFT JOIN got USING (chat_id, message_id) WHERE coalesce(got.n, 0) <> want.n
    """)
    bad_max = ctx.one("""
        SELECT count(*) FROM max_messages m WHERE NOT EXISTS (
            SELECT 1 FROM messages nm JOIN chats ch ON ch.account_id = nm.account_id AND ch.id = nm.chat_id
            WHERE nm.account_id = %(max)s AND ch.external_id = m.chat_id AND nm.external_id = m.message_id)
    """) if ctx.max is not None else 0
    unassigned = ctx.one("SELECT coalesce(sum(messages), 0) FROM mig_unassigned")
    return {"ok": bad_tg == 0 and bad_max == 0, "tg_mismatch": bad_tg, "max_missing": bad_max,
            "unassigned_messages": unassigned}


def _v2(ctx: Ctx) -> dict:
    bad = ctx.one(f"""
        SELECT count(*) FROM messages nm
        JOIN chats ch ON ch.account_id = nm.account_id AND ch.id = nm.chat_id AND ch.chat_type IN {PRIVATE}
        JOIN users us ON us.account_id = nm.account_id AND us.id = nm.sender_user_id
        JOIN accounts self ON self.platform = 'telegram' AND self.external_user_id = us.external_id
        WHERE nm.raw->>'out' = 'true' AND self.id <> nm.account_id
    """)
    return {"ok": bad == 0, "mismatch": bad}


def _v3(ctx: Ctx) -> dict:
    chats = set(V3_CHATS)
    for (ids,) in ctx.rows("SELECT watch_chat_ids FROM accounts WHERE id = %(high)s"):
        chats |= set(ids or [])
    chats |= {r[0] for r in ctx.rows("SELECT chat_id FROM chat_access WHERE account_id = %(high)s")}
    bad = {}
    # First legacy media row of every message, once (tg_media has no index on message_pk).
    ctx.q("DROP TABLE IF EXISTS mig_first_media")
    ctx.q("""CREATE TEMP TABLE mig_first_media AS
             SELECT DISTINCT ON (message_pk) message_pk, file_name, file_size FROM tg_media
             ORDER BY message_pk, id""")
    ctx.q("CREATE INDEX ON mig_first_media (message_pk)")
    skipped = []
    for chat in sorted(chats):
        if ctx.one("SELECT 1 FROM mig_unassigned WHERE legacy_chat_id = %(c)s", {"c": chat}):
            skipped.append(chat)
            continue
        old = set(ctx.rows("""
            SELECT m.message_id, m.message_type, md.file_name, md.file_size FROM tg_messages m
            LEFT JOIN mig_first_media md ON md.message_pk = m.id
            WHERE m.chat_id = %(c)s""", {"c": chat}))
        new = set(ctx.rows("""
            SELECT nm.external_id, nm.message_type, md.file_name, md.file_size FROM messages nm
            JOIN chats ch ON ch.account_id = nm.account_id AND ch.id = nm.chat_id
            LEFT JOIN media md ON md.account_id = nm.account_id AND md.message_id = nm.id AND md.attach_index = 0
            WHERE nm.account_id = %(high)s AND ch.external_id = %(c)s""", {"c": chat}))
        if old and old != new:
            bad[chat] = {"missing": len(old - new), "extra": len(new - old)}
    return {"ok": not bad, "chats": len(chats), "compared": len(chats) - len(skipped),
            "skipped_unassigned": skipped, "differ": bad}


def _v4(ctx: Ctx) -> dict:
    rows = ctx.rows("""
        SELECT a.alias, count(u.id) FILTER (WHERE u.is_self) FROM accounts a
        LEFT JOIN users u ON u.account_id = a.id WHERE a.is_enabled GROUP BY a.alias ORDER BY a.alias
    """)
    bad = {alias: n for alias, n in rows if n != 1}
    return {"ok": not bad, "not_one": bad}


def _v5(ctx: Ctx) -> dict:
    missing = [r[0] for r in ctx.rows("SELECT alias FROM accounts WHERE is_enabled AND external_user_id IS NULL")]
    return {"ok": not missing, "missing": missing}


def _v6(ctx: Ctx, n: int = 200) -> dict:
    ids = [r[0] for r in ctx.rows("""
        SELECT m.id FROM tg_messages m WHERE NOT EXISTS
          (SELECT 1 FROM mig_unassigned u WHERE u.legacy_chat_id = m.chat_id)
        ORDER BY random() LIMIT %(n)s""", {"n": n})]
    bad = 0
    for legacy_id in ids:
        bad += ctx.one("""
            SELECT count(*) FROM tg_messages m
            JOIN chats ch ON ch.external_id = m.chat_id
            JOIN messages nm ON nm.account_id = ch.account_id AND nm.chat_id = ch.id AND nm.external_id = m.message_id
            WHERE m.id = %(i)s AND (nm.text IS DISTINCT FROM m.text OR nm.sent_at <> m.tg_date
                                    OR nm.edited_at IS DISTINCT FROM m.edit_date)""", {"i": legacy_id})
    return {"ok": bad == 0, "sampled": len(ids), "differ": bad}


def _v7(ctx: Ctx) -> dict:
    expected = ctx.one("""
        SELECT count(*) FROM (
            SELECT 'telegram', id FROM tg_users
            UNION SELECT 'telegram', tg_user_id FROM users_registry WHERE tg_user_id IS NOT NULL
            UNION SELECT 'telegram', from_user_id FROM tg_messages WHERE from_user_id IS NOT NULL
            UNION SELECT 'telegram', tg_user_id FROM chat_member_records WHERE tg_user_id IS NOT NULL
            UNION SELECT 'max', id FROM max_users
            UNION SELECT 'max', from_user_id FROM max_messages WHERE from_user_id IS NOT NULL
            UNION SELECT platform, external_user_id FROM accounts WHERE external_user_id IS NOT NULL
        ) s""")
    have = ctx.one("SELECT count(*) FROM identities")
    orphans = ctx.one("SELECT count(*) FROM people p WHERE NOT EXISTS"
                      " (SELECT 1 FROM identities i WHERE i.person_id = p.id)")
    return {"ok": have >= expected and orphans == 0, "expected_at_least": expected, "identities": have,
            "people_without_identities": orphans}


__all__ = ["Options", "PreconditionFailed", "copy", "delta", "verify"]
