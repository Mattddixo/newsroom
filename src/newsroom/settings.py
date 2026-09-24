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
    trusted_proxies: list[str] = field(default_factory=list)  # CIDRs allowed to set client IP
    # Worker
    config_dir: Path = Path("/app/config")
    backup_hour: int = 3
    backup_keep: int = 14
    ingest_interval_minutes: int = 15  # GDELT updates every 15 minutes
    ingest_offset_minutes: int = 3  # run this long after each GDELT update
    ingest_backfill_hours: int = 48
    retention_days: int = 365
    gdelt_group_size: int = 8
    gdelt_min_interval: float = 10.0
    ownership_refresh_days: int = 7
    pubdate_fetch: bool = True  # read publication dates from article pages
    pubdate_per_run: int = 150
    pubdate_max_age_days: int = 1
    wikidata_min_interval: float = 1.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "newsroom.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logo_dir(self) -> Path:
        return self.data_dir / "logos"

    def lock_path(self, job: str) -> Path:
        """One lock per kind of job ("ingest", "records", "pubdates"), so a slow GDELT
        run never holds up ownership, funding or publication dates."""
        return self.data_dir / "db" / f"{job}.lock"

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
        trusted_proxies=_list(env.get("TRUSTED_PROXIES")),
        config_dir=Path(env.get("NEWSROOM_CONFIG_DIR", "/app/config")),
        backup_hour=int(env.get("BACKUP_HOUR", "3")),
        backup_keep=int(env.get("BACKUP_KEEP", "14")),
        ingest_interval_minutes=int(env.get("INGEST_INTERVAL_MINUTES", "15")),
        ingest_offset_minutes=int(env.get("INGEST_OFFSET_MINUTES", "3")),
        ingest_backfill_hours=int(env.get("INGEST_BACKFILL_HOURS", "48")),
        retention_days=int(env.get("RETENTION_DAYS", "365")),
        gdelt_group_size=int(env.get("GDELT_GROUP_SIZE", "8")),
        gdelt_min_interval=float(env.get("GDELT_MIN_INTERVAL", "10")),
        ownership_refresh_days=int(env.get("OWNERSHIP_REFRESH_DAYS", "7")),
        pubdate_fetch=_bool(env.get("PUBDATE_FETCH"), True),
        pubdate_per_run=int(env.get("PUBDATE_PER_RUN", "150")),
        pubdate_max_age_days=int(env.get("PUBDATE_MAX_AGE_DAYS", "1")),
        wikidata_min_interval=float(env.get("WIKIDATA_MIN_INTERVAL", "1")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
