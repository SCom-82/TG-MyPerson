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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }


settings = Settings()
