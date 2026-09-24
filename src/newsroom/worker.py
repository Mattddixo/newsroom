"""Background worker: the only long-running process that writes to the database.

Jobs are added here phase by phase (ingestion, ownership refresh). Each job is
single-instance and coalesced, so a slow run never overlaps the next one.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from newsroom.backup import backup
from newsroom.db import connect
from newsroom.log import setup_logging
from newsroom.migrate import migrate
from newsroom.settings import Settings, get_settings

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/newsroom-worker.heartbeat")  # noqa: S108 - tmpfs inside the container
HEARTBEAT_MAX_AGE = 180


def beat(path: Path = HEARTBEAT) -> None:
    path.write_text(str(int(time.time())))


def heartbeat_ok(path: Path = HEARTBEAT, max_age: int = HEARTBEAT_MAX_AGE) -> bool:
    try:
        return time.time() - int(path.read_text()) < max_age
    except (OSError, ValueError):
        return False


def run_backup(settings: Settings) -> None:
    try:
        backup(settings.db_path, settings.backup_dir, settings.backup_keep)
    except Exception:
        log.exception("backup failed")


def build_scheduler(settings: Settings) -> BlockingScheduler:
    scheduler = BlockingScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    scheduler.add_job(beat, IntervalTrigger(seconds=30), id="heartbeat")
    scheduler.add_job(
        run_backup,
        CronTrigger(hour=settings.backup_hour, minute=0, timezone=settings.timezone),
        args=[settings],
        id="backup",
    )
    return scheduler


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    log.info("worker starting", extra={"migrations_applied": applied})
    beat()
    scheduler = build_scheduler(settings)
    for job in scheduler.get_jobs():
        log.info("scheduled job", extra={"job": job.id, "trigger": str(job.trigger)})
    scheduler.start()
