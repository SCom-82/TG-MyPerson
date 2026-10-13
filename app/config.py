from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/tg_myperson"
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone_number: str = ""
    tg_session_string: str = ""
    api_key: str = ""
    # Admin API key for /api/v1/accounts/* endpoints.
    # Env var: TG_ADMIN_API_KEY.
    # If empty — admin endpoints return 503 (misconfigured, not 401, to avoid enumeration).
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    admin_api_key: str = Field(default="", alias="TG_ADMIN_API_KEY")
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    user_timezone: str = "Europe/Samara"

    # Account isolation (ТЗ 2026-10-13 §4.3). legacy: tg_*/max_* only, the new
    # per-account tables are neither written nor read. new: write + read the
    # new tables, shadow-write the old ones while SHADOW_LEGACY_WRITE is on.
    storage_mode: Literal["legacy", "new"] = "legacy"
    shadow_legacy_write: bool = True
    # Daily address-book sync of TG accounts (new mode).
    contacts_sync_hours: float = 24
    # §6.1 / D6, D10: the shared API_KEY keeps working while this is true;
    # without X-Session-Alias it means 'work' unless REQUIRE_ACCOUNT=true (→ 400).
    legacy_api_key_enabled: bool = True
    # Switch to STORAGE_MODE=new (§11.2 step 5): migrate_isolation delta before
    # sessions connect, then the cutover mark. Remove after the switch.
    run_delta_on_start: bool = False
    require_account: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }


settings = Settings()
