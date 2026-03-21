from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""

    supabase_url: str = ""
    supabase_service_role_key: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"

    timezone: str = "Asia/Hong_Kong"
    daily_push_time: str = "09:00"
    max_daily_tasks: int = 6
    cron_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("max_daily_tasks", mode="before")
    @classmethod
    def normalize_max_daily_tasks(cls, value: object) -> int:
        if value in (None, ""):
            return 6
        return int(value)

    @property
    def supabase_rest_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/rest/v1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
