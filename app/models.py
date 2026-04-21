from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TgUser(Base):
    __tablename__ = "tg_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_tg_users_username", "username"),
    )


class TgChat(Base):
    __tablename__ = "tg_chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    members_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["TgMessage"]] = relationship("TgMessage", back_populates="chat", lazy="noload")

    __table_args__ = (
        Index("ix_tg_chats_chat_type", "chat_type"),
        Index("ix_tg_chats_username", "username"),
        Index("ix_tg_chats_is_monitored", "is_monitored"),
    )


class TgMessage(Base):
    __tablename__ = "tg_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_chats.id"), nullable=False)
    from_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tg_users.id"), nullable=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    forward_from_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    forward_from_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_type: Mapped[str] = mapped_column(String(30), default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["TgChat"] = relationship("TgChat", back_populates="messages")
    sender: Mapped["TgUser | None"] = relationship("TgUser", lazy="selectin")
    media: Mapped[list["TgMedia"]] = relationship("TgMedia", back_populates="message", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("message_id", "chat_id", name="uq_tg_messages_msg_chat"),
        Index("ix_tg_messages_chat_date", "chat_id", "tg_date"),
        Index("ix_tg_messages_from_user", "from_user_id"),
        Index("ix_tg_messages_type", "message_type"),
    )


class TgMedia(Base):
    __tablename__ = "tg_media"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_messages.id"), nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_type: Mapped[str] = mapped_column(String(30), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["TgMessage"] = relationship("TgMessage", back_populates="media")


class TgSession(Base):
    # DEPRECATED — dropped in migration 003 (Phase 4 of multi-account refactor).
    # Data migrated to accounts + account_sessions in migration 002.
    __tablename__ = "tg_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    session_string: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TgSyncState(Base):
    __tablename__ = "tg_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_chats.id"), unique=True, nullable=False)
    oldest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    newest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_fully_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    total_messages_synced: Mapped[int] = mapped_column(Integer, default=0)
    last_backfill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Multi-account models (Phase 1 of multi-account refactor, migration 002)
# ---------------------------------------------------------------------------


class Account(Base):
    """Описание залогиненного Telegram-аккаунта (work, personal-ro, ...)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)  # 'rw' | 'ro'
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    watch_chat_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    sessions: Mapped[list["AccountSession"]] = relationship(
        "AccountSession", back_populates="account", lazy="noload"
    )
    tool_policies: Mapped[list["AccountToolPolicy"]] = relationship(
        "AccountToolPolicy", back_populates="account", lazy="noload"
    )

    __table_args__ = (UniqueConstraint("alias", name="uq_accounts_alias"),)


class AccountSession(Base):
    """Хранение сессионной строки Telethon. Phase 1-4: plaintext; Phase 5+: ciphertext."""

    __tablename__ = "account_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    session_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    encryption_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="sessions")

    __table_args__ = (Index("ix_account_sessions_account_id", "account_id"),)


class AccountToolPolicy(Base):
    """Тонкая настройка allow/deny per-tool per-account (опционально)."""

    __tablename__ = "account_tool_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    effect: Mapped[str] = mapped_column(Text, nullable=False)  # 'allow' | 'deny'
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="tool_policies")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "tool_name", name="uq_account_tool_policy_account_tool"
        ),
    )


class ChatAccess(Base):
    """Связка account ↔ chat: какой аккаунт видит какой чат."""

    __tablename__ = "chat_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_chats.id"), nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)  # 'member' | 'admin' | 'creator'

    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", name="uq_chat_access_account_chat"),
        Index("ix_chat_access_account_id", "account_id"),
        Index("ix_chat_access_chat_id", "chat_id"),
    )


class ChatMembersSnapshot(Base):
    """Исторический слепок состава чата."""

    __tablename__ = "chat_members_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_chats.id"), nullable=False)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    members_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)  # 'api' | 'manual-import' | 'screenshot'
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    records: Mapped[list["ChatMemberRecord"]] = relationship(
        "ChatMemberRecord", back_populates="snapshot", lazy="noload"
    )

    __table_args__ = (
        Index("ix_chat_members_snapshots_chat_taken", "chat_id", "taken_at"),
    )


class ChatMemberRecord(Base):
    """Участник в конкретном снапшоте (плоский список для SQL-запросов)."""

    __tablename__ = "chat_member_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chat_members_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    snapshot: Mapped["ChatMembersSnapshot"] = relationship(
        "ChatMembersSnapshot", back_populates="records"
    )

    __table_args__ = (
        Index("ix_chat_member_records_snapshot_id", "snapshot_id"),
        Index("ix_chat_member_records_tg_user_id", "tg_user_id"),
        Index("ix_chat_member_records_username", "username"),
    )


class UsersRegistry(Base):
    """Канонический человек, агрегирован через все источники."""

    __tablename__ = "users_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    primary_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    sources: Mapped[list["UsersRegistrySource"]] = relationship(
        "UsersRegistrySource", back_populates="registry_entry", lazy="noload"
    )

    __table_args__ = (
        Index("ix_users_registry_tg_user_id", "tg_user_id"),
        Index("ix_users_registry_primary_username", "primary_username"),
        Index("ix_users_registry_primary_phone", "primary_phone"),
    )


class UsersRegistrySource(Base):
    """Откуда приехал факт о человеке (снапшот, сообщение, ручной импорт)."""

    __tablename__ = "users_registry_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users_registry.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # 'snapshot' | 'tg_messages' | 'manual'
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    registry_entry: Mapped["UsersRegistry"] = relationship(
        "UsersRegistry", back_populates="sources"
    )

    __table_args__ = (
        Index("ix_users_registry_sources_registry_id", "registry_id"),
        Index("ix_users_registry_sources_account_observed", "account_id", "observed_at"),
    )


class AuditLog(Base):
    """Журнал всех вызовов MCP-тулов. Партиционирован по ts (RANGE).

    Note: физически таблица создаётся через op.execute() в миграции 002
    (PARTITION BY RANGE), не через стандартный create_table alembic.
    SQLAlchemy-класс используется только для ORM-запросов (SELECT/INSERT).
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, primary_key=True
    )
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    is_write: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    params_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)  # 'ok' | 'denied' | 'error'
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Таблица управляется как партиционированная — индексы созданы в миграции.
        # Класс не участвует в alembic autogenerate.
        {"info": {"skip_autogenerate": True}},
    )
