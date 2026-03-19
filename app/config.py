from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/tg_myperson"
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone_number: str = ""
    tg_session_string: str = ""
    api_key: str = ""
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    user_timezone: str = "Europe/Samara"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
