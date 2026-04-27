"""Multi-account schema: accounts, sessions, snapshots, registry, audit

Revision ID: 002
Revises: 001
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # accounts — описание залогиненных сессий
    # ------------------------------------------------------------------
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("phone", sa.Text, nullable=False),
        sa.Column("tg_user_id", sa.BigInteger, nullable=True),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column(
            "watch_chat_ids",
            ARRAY(sa.BigInteger),
            nullable=True,
        ),
        sa.Column("is_enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_unique_constraint("uq_accounts_alias", "accounts", ["alias"])
    op.execute(
        "ALTER TABLE accounts ADD CONSTRAINT ck_accounts_mode "
        "CHECK (mode IN ('rw', 'ro'))"
    )

    # ------------------------------------------------------------------
    # account_sessions — хранение сессионных строк (plaintext, Phase 1-4)
    # ------------------------------------------------------------------
    op.create_table(
        "account_sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_plaintext", sa.Text, nullable=True),
        sa.Column("session_ciphertext", BYTEA, nullable=True),
        sa.Column("encryption_key_id", sa.Text, nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
    )
    # Частичный UNIQUE: только одна активная сессия на аккаунт
    op.execute(
        "CREATE UNIQUE INDEX uq_account_sessions_one_active "
        "ON account_sessions (account_id) WHERE is_active = true"
    )
    op.create_index("ix_account_sessions_account_id", "account_sessions", ["account_id"])

    # ------------------------------------------------------------------
    # account_tool_policy — тонкая настройка allow/deny per-tool per-account
    # ------------------------------------------------------------------
    op.create_table(
        "account_tool_policy",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("effect", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_unique_constraint(
        "uq_account_tool_policy_account_tool",
        "account_tool_policy",
        ["account_id", "tool_name"],
    )
    op.execute(
        "ALTER TABLE account_tool_policy ADD CONSTRAINT ck_account_tool_policy_effect "
        "CHECK (effect IN ('allow', 'deny'))"
    )

    # ------------------------------------------------------------------
    # chat_access — связка account ↔ chat
    # ------------------------------------------------------------------
    op.create_table(
        "chat_access",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "chat_id",
            sa.BigInteger,
            sa.ForeignKey("tg_chats.id"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.Text, nullable=True),
    )
    op.create_unique_constraint(
        "uq_chat_access_account_chat", "chat_access", ["account_id", "chat_id"]
    )
    op.create_index("ix_chat_access_account_id", "chat_access", ["account_id"])
    op.create_index("ix_chat_access_chat_id", "chat_access", ["chat_id"])

    # ------------------------------------------------------------------
    # chat_members_snapshots — исторические слепки состава чата
    # ------------------------------------------------------------------
    op.create_table(
        "chat_members_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "chat_id",
            sa.BigInteger,
            sa.ForeignKey("tg_chats.id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id"),
            nullable=True,
        ),
        sa.Column(
            "taken_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("members_count", sa.Integer, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_chat_members_snapshots_chat_taken",
        "chat_members_snapshots",
        ["chat_id", "taken_at"],
    )

    # ------------------------------------------------------------------
    # chat_member_records — участники в конкретном снапшоте
    # ------------------------------------------------------------------
    op.create_table(
        "chat_member_records",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_id",
            sa.Integer,
            sa.ForeignKey("chat_members_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tg_user_id", sa.BigInteger, nullable=True),
        sa.Column("username", sa.Text, nullable=True),
        sa.Column("first_name", sa.Text, nullable=True),
        sa.Column("last_name", sa.Text, nullable=True),
        sa.Column("phone", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=True),
        sa.Column("raw", JSONB, nullable=True),
    )
    op.create_index(
        "ix_chat_member_records_snapshot_id", "chat_member_records", ["snapshot_id"]
    )
    op.create_index(
        "ix_chat_member_records_tg_user_id", "chat_member_records", ["tg_user_id"]
    )
    op.create_index(
        "ix_chat_member_records_username", "chat_member_records", ["username"]
    )

    # ------------------------------------------------------------------
    # users_registry — агрегированный реестр контактов
    # ------------------------------------------------------------------
    op.create_table(
        "users_registry",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tg_user_id", sa.BigInteger, unique=True, nullable=True),
        sa.Column("primary_username", sa.Text, nullable=True),
        sa.Column("primary_name", sa.Text, nullable=True),
        sa.Column("primary_phone", sa.Text, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tags", ARRAY(sa.Text), nullable=True),
    )
    op.create_index(
        "ix_users_registry_tg_user_id", "users_registry", ["tg_user_id"]
    )
    op.create_index(
        "ix_users_registry_primary_username", "users_registry", ["primary_username"]
    )
    op.create_index(
        "ix_users_registry_primary_phone", "users_registry", ["primary_phone"]
    )
    op.execute(
        "CREATE INDEX ix_users_registry_tags_gin ON users_registry USING GIN (tags)"
    )

    # ------------------------------------------------------------------
    # users_registry_sources — откуда приехал факт
    # ------------------------------------------------------------------
    op.create_table(
        "users_registry_sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "registry_id",
            sa.Integer,
            sa.ForeignKey("users_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.Text, nullable=True),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id"),
            nullable=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_fields", JSONB, nullable=True),
    )
    op.create_index(
        "ix_users_registry_sources_registry_id",
        "users_registry_sources",
        ["registry_id"],
    )
    op.create_index(
        "ix_users_registry_sources_account_observed",
        "users_registry_sources",
        ["account_id", "observed_at"],
    )

    # ------------------------------------------------------------------
    # audit_logs — партиционированная таблица логов (PARTITION BY RANGE ts)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE audit_logs (
            id         BIGSERIAL,
            ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
            account_id INTEGER,
            alias      TEXT,
            tool       TEXT NOT NULL,
            is_write   BOOLEAN,
            chat_id    BIGINT,
            target_user_id BIGINT,
            params_digest  TEXT,
            status     TEXT,
            error      TEXT,
            PRIMARY KEY (id, ts)
        ) PARTITION BY RANGE (ts)
        """
    )

    # FK на accounts нельзя ставить на партиционированную таблицу напрямую —
    # партиции наследуют ограничения при CREATE TABLE ... PARTITION OF.
    # Индексы на родительской таблице создаются так же через op.execute.
    op.execute(
        "CREATE INDEX ix_audit_logs_ts ON audit_logs (ts DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_account_ts ON audit_logs (account_id, ts DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_tool_ts ON audit_logs (tool, ts)"
    )

    # Партиция текущего месяца (2026-04)
    op.execute(
        """
        CREATE TABLE audit_logs_2026_04 PARTITION OF audit_logs
        FOR VALUES FROM ('2026-04-01') TO ('2026-05-01')
        """
    )
    # Партиция следующего месяца (2026-05)
    op.execute(
        """
        CREATE TABLE audit_logs_2026_05 PARTITION OF audit_logs
        FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
        """
    )

    # ------------------------------------------------------------------
    # Бэкфилл: перенести активную строку tg_session → accounts + account_sessions
    # Если tg_session пустой — INSERT'ы ничего не сделают.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO accounts (alias, mode, phone, tg_user_id, display_name)
        SELECT 'work', 'rw', s.phone_number,
               NULL,
               'SComITBus (work)'
        FROM tg_session s
        WHERE s.is_active = true
        LIMIT 1
        """
    )

    op.execute(
        """
        INSERT INTO account_sessions (account_id, session_plaintext, authorized_at, last_connected_at, is_active)
        SELECT a.id, s.session_string, s.last_connected_at, s.last_connected_at, true
        FROM tg_session s
        JOIN accounts a ON a.alias = 'work'
        WHERE s.is_active = true
        LIMIT 1
        """
    )


def downgrade() -> None:
    # audit_logs (партиционированная) — партиции дропаются автоматически
    op.execute("DROP TABLE IF EXISTS audit_logs CASCADE")

    op.drop_table("users_registry_sources")
    op.drop_table("users_registry")
    op.drop_table("chat_member_records")
    op.drop_table("chat_members_snapshots")
    op.drop_table("chat_access")
    op.drop_table("account_tool_policy")
    op.drop_table("account_sessions")
    op.drop_table("accounts")
