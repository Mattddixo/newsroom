"""Background worker: the only long-running process that writes to the database.

Jobs are added here phase by phase (ingestion, ownership refresh). Each job is
single-instance and coalesced, so a slow run never overlaps the next one.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from newsroom import jobs
from newsroom.backup import backup
from newsroom.db import connect
from newsroom.log import setup_logging
from newsroom.migrate import migrate
from newsroom.services.ingest import IngestBusy
from newsroom.settings import Settings, get_settings

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/newsroom-worker.heartbeat")  # noqa: S108 - tmpfs inside the container
HEARTBEAT_MAX_AGE = 180
# Each kind of job has its own lock, so these waits only matter when the same kind is
# already running from the CLI (e.g. `make ownership` during the scheduled run).
INGEST_WAIT = 30 * 60
RECORDS_WAIT = 90 * 60
PUBDATES_WAIT = 0  # runs every 15 minutes anyway


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


def run_ingest(settings: Settings) -> None:
    try:
        jobs.ingest_articles(settings, wait=INGEST_WAIT)
    except IngestBusy:
        log.warning("ingest skipped: another ingest held the lock for 30 minutes")
    except Exception:
        log.exception("ingest failed")


def run_ownership(settings: Settings) -> None:
    try:
        jobs.resolve_ownership(settings, wait=RECORDS_WAIT)
        jobs.refresh_funding(settings, wait=RECORDS_WAIT)
    except IngestBusy:
        log.warning("ownership/funding refresh skipped: another one held the lock for 90 minutes")
    except Exception:
        log.exception("ownership/funding refresh failed")


def run_pubdates(settings: Settings) -> None:
    try:
        jobs.publication_dates(settings, wait=PUBDATES_WAIT)
    except IngestBusy:
        log.info("publication dates skipped: a previous pass is still running")
    except Exception:
        log.exception("publication dates failed")


def run_prune(settings: Settings) -> None:
    try:
        jobs.prune(settings)
    except Exception:
        log.exception("prune failed")


def ingest_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    """Run on the clock, a few minutes after GDELT publishes (it updates at :00, :15,
    :30 and :45 UTC). Every 15 min with offset 3 -> :03, :18, :33, :48. Intervals that
    don't divide an hour or a day fall back to a plain timer."""
    every = max(1, settings.ingest_interval_minutes)
    offset = settings.ingest_offset_minutes % min(every, 60)
    if every < 60 and 60 % every == 0:
        return CronTrigger(minute=f"{offset}-59/{every}", timezone="UTC")
    if every % 60 == 0 and 1440 % every == 0:  # hourly, every 2 h, ...
        return CronTrigger(hour=f"*/{every // 60}", minute=offset, timezone="UTC")
    return IntervalTrigger(minutes=every)


def pubdates_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    """Same cadence as ingestion, 5 minutes later, so a fresh batch is usually waiting.
    Independent of it: a slow or failing GDELT run doesn't hold dates back."""
    base = ingest_trigger(settings)
    every = max(1, settings.ingest_interval_minutes)
    if isinstance(base, CronTrigger) and every < 60:
        offset = (settings.ingest_offset_minutes + 5) % every
        return CronTrigger(minute=f"{offset}-59/{every}", timezone="UTC")
    return IntervalTrigger(minutes=min(every, 15))


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
    scheduler.add_job(
        run_ingest,
        ingest_trigger(settings),
        args=[settings],
        id="ingest",
        next_run_time=datetime.now().astimezone() + timedelta(seconds=30),
    )
    scheduler.add_job(
        run_pubdates,
        pubdates_trigger(settings),
        args=[settings],
        id="pubdates",
        next_run_time=datetime.now().astimezone() + timedelta(minutes=1),
    )
    # Only outlets not checked within OWNERSHIP_REFRESH_DAYS are re-resolved on each run.
    scheduler.add_job(
        run_ownership,
        IntervalTrigger(hours=6),
        args=[settings],
        id="ownership",
        next_run_time=datetime.now().astimezone() + timedelta(minutes=3),
    )
    scheduler.add_job(
        run_prune,
        CronTrigger(hour=settings.backup_hour, minute=30, timezone=settings.timezone),
        args=[settings],
        id="prune",
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
