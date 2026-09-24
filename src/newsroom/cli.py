"""Command-line interface: `newsroom <command>`.

Run inside the worker container, e.g. `docker compose exec worker newsroom backup`.
Curation commands (outlets, ownership) are added in later phases.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

from newsroom import __version__
from newsroom.backup import backup
from newsroom.db import connect
from newsroom.log import setup_logging
from newsroom.migrate import migrate
from newsroom.settings import get_settings


def cmd_migrate(_: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    print(f"Applied: {applied}" if applied else "Database is up to date.")
    return 0


def cmd_backup(_: argparse.Namespace) -> int:
    settings = get_settings()
    path = backup(settings.db_path, settings.backup_dir, settings.backup_keep)
    print(f"Backup written: {path}")
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    from newsroom.worker import main as worker_main

    worker_main()
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    """Container healthcheck. `web` probes /healthz; `worker` checks its heartbeat."""
    if args.target == "worker":
        from newsroom.worker import heartbeat_ok

        return 0 if heartbeat_ok() else 1
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3) as resp:
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsroom", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(
        func=cmd_migrate
    )
    sub.add_parser("backup", help="write a consistent SQLite snapshot now").set_defaults(
        func=cmd_backup
    )
    sub.add_parser("worker", help="run the scheduler (used by the worker container)").set_defaults(
        func=cmd_worker
    )
    health = sub.add_parser("healthcheck", help="exit 0 if healthy (container healthcheck)")
    health.add_argument("target", choices=["web", "worker"])
    health.set_defaults(func=cmd_healthcheck)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command != "healthcheck":
        setup_logging(get_settings().log_level)
    sys.exit(args.func(args))
