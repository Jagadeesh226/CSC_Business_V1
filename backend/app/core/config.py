from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Auth and cookie settings, read from environment variables.

    COOKIE_SECURE defaults to true (cookies only over HTTPS). For local HTTP
    development set COOKIE_SECURE=false.
    """

    model_config = SettingsConfigDict(extra="ignore")

    session_ttl_hours: int = 12
    cookie_secure: bool = True
    session_cookie_name: str = "csc_session"
    csrf_cookie_name: str = "csc_csrf"
    csrf_header_name: str = "X-CSRF-Token"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
