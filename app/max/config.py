"""MAX adapter settings (API spec §6). Env names are the spec's, no prefix magic."""

from pydantic import Field
from pydantic_settings import BaseSettings


class MaxSettings(BaseSettings):
    enabled: bool = Field(default=False, alias="MAX_ENABLED")
    # socks5://user:pass@host:port or http://… — all MAX traffic (WS/TCP + media).
    proxy_url: str = Field(default="", alias="MAX_PROXY_URL")
    # Fail-closed: no proxy → the session does not start (owner's decision 07.10).
    require_proxy: bool = Field(default=True, alias="MAX_REQUIRE_PROXY")
    catchup_seed: int = Field(default=50, alias="MAX_CATCHUP_SEED")
    catchup_max_chats: int = Field(default=60, alias="MAX_CATCHUP_MAX_CHATS")
    raw_events_retention_days: int = Field(default=30, alias="MAX_RAW_EVENTS_RETENTION_DAYS")
    # Scheduled messages (plan 2026-10-22 Р5 Р-73): false → schedule_date and the three scheduled
    # tools answer 501 and leave GET /tools, without a rollback (MAX's server flag may be off).
    scheduled_enabled: bool = Field(default=True, alias="MAX_SCHEDULED_ENABLED")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
        "extra": "ignore",
    }


max_settings = MaxSettings()
