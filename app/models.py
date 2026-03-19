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
from sqlalchemy.dialects.postgresql import JSONB
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
