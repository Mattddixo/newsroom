"""Runtime configuration, read once from environment variables.

Every setting has a working default except CONTACT_EMAIL, which outbound API
clients require (Wikimedia and SEC ask for a real contact in the User-Agent).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    timezone: str = "America/Toronto"
    log_level: str = "INFO"
    contact_email: str = ""
    # Web
    rate_limit: str = "120/minute"
    search_rate_limit: str = "30/minute"
    enable_hsts: bool = False
    allowed_hosts: list[str] = field(default_factory=lambda: ["*"])
    # Worker
    backup_hour: int = 3
    backup_keep: int = 14

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "newsroom.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logo_dir(self) -> Path:
        return self.data_dir / "logos"

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "no-contact-configured"
        return f"newsroom/0.1 (self-hosted news ownership viewer; {contact})"


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        data_dir=Path(env.get("NEWSROOM_DATA_DIR", "/data")),
        timezone=env.get("TZ", "America/Toronto"),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        contact_email=env.get("CONTACT_EMAIL", "").strip(),
        rate_limit=env.get("RATE_LIMIT", "120/minute"),
        search_rate_limit=env.get("SEARCH_RATE_LIMIT", "30/minute"),
        enable_hsts=_bool(env.get("ENABLE_HSTS"), False),
        allowed_hosts=_list(env.get("ALLOWED_HOSTS")) or ["*"],
        backup_hour=int(env.get("BACKUP_HOUR", "3")),
        backup_keep=int(env.get("BACKUP_KEEP", "14")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
