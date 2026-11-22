from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read from environment variables.

    COOKIE_SECURE defaults to true (cookies only over HTTPS). For local HTTP
    development set COOKIE_SECURE=false.
    """

    model_config = SettingsConfigDict(extra="ignore")

    session_ttl_hours: int = 12
    cookie_secure: bool = True
    session_cookie_name: str = "csc_session"
    csrf_cookie_name: str = "csc_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    # Printed at the top of every receipt (CENTRE_NAME, CENTRE_ADDRESS,
    # CENTRE_PHONE). Defaults keep receipts printable before they are set.
    centre_name: str = "Common Service Centre"
    centre_address: str = ""
    centre_phone: str = ""
    # Optional TTF font for receipts (RECEIPT_FONT_PATH), for names in
    # characters the built-in font cannot print.
    receipt_font_path: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
