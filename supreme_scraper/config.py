"""
config.py — Single source of truth for all application settings.

Loads values from the .env file via python-dotenv.
Sensitive credentials (Telegram token/chat ID) come from environment only —
never from source code.

SECURITY: No `verify`, `tls_verify`, or `ssl_verify` field is exposed here.
TLS enforcement is hardcoded unconditionally in crawler.py and cannot be
disabled through configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import certifi
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------ #
    # Target — fixed at design time, not user-configurable                #
    # ------------------------------------------------------------------ #
    TARGET_URL: str = "https://supreme.com/previews/springsummer2026/all"
    ROBOTS_URL: str = "https://supreme.com/robots.txt"
    SOURCE_WEBSITE: str = "supreme.com"

    # ------------------------------------------------------------------ #
    # HTTP behaviour                                                       #
    # ------------------------------------------------------------------ #
    USER_AGENT: str = field(
        default_factory=lambda: os.getenv(
            "USER_AGENT",
            "SupremeScraper/1.0 (InfoSec Course Research Bot)",
        )
    )
    REQUEST_TIMEOUT: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    )

    # ------------------------------------------------------------------ #
    # Scheduling                                                           #
    # ------------------------------------------------------------------ #
    SCRAPE_INTERVAL_MINUTES: int = field(
        default_factory=lambda: int(os.getenv("SCRAPE_INTERVAL_MINUTES", "15"))
    )

    # ------------------------------------------------------------------ #
    # Database                                                             #
    # ------------------------------------------------------------------ #
    DATABASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "sqlite+aiosqlite:///./supreme_drops.db",
        )
    )

    # ------------------------------------------------------------------ #
    # Credentials — read from env only, never hardcoded                   #
    # ------------------------------------------------------------------ #
    
    DISCORD_WEBHOOK_URL: str = field(
    default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", "")
    )

    # ------------------------------------------------------------------ #
    # Logging                                                              #
    # ------------------------------------------------------------------ #
    LOG_LEVEL: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # ------------------------------------------------------------------ #
    # TLS — read-only reference, not a toggle                             #
    # The CA bundle path is resolved here so certifi version is auditable #
    # but verify=True is hardcoded in crawler.py and cannot be changed.  #
    # ------------------------------------------------------------------ #
    CA_BUNDLE: str = field(default_factory=certifi.where)

    def __post_init__(self) -> None:
        if not self.DATABASE_URL.startswith("sqlite+aiosqlite://"):
            raise ValueError(
                "DATABASE_URL must use the sqlite+aiosqlite:// scheme. "
                "Other database backends are not supported in this prototype."
            )

        allowed_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.LOG_LEVEL not in allowed_levels:
            raise ValueError(
                f"LOG_LEVEL must be one of {allowed_levels}, got: {self.LOG_LEVEL!r}"
            )

        if not (1.0 <= self.REQUEST_TIMEOUT <= 60.0):
            raise ValueError(
                f"REQUEST_TIMEOUT_SECONDS must be between 1 and 60, "
                f"got: {self.REQUEST_TIMEOUT}"
            )

        if not (5 <= self.SCRAPE_INTERVAL_MINUTES <= 1440):
            raise ValueError(
                f"SCRAPE_INTERVAL_MINUTES must be between 5 and 1440, "
                f"got: {self.SCRAPE_INTERVAL_MINUTES}"
            )


# Module-level singleton — import this from all other modules.
settings = Settings()
