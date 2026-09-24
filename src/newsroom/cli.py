"""Command-line interface: `newsroom <command>`.

Run inside the worker container, e.g. `docker compose exec worker newsroom backup`.
Curation commands (outlets, ownership) are added in later phases.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

from newsroom import __version__, jobs
from newsroom.backup import backup
from newsroom.config import ConfigError, load_outlets, load_tags
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


def cmd_ingest(_: argparse.Namespace) -> int:
    s = jobs.ingest_articles(get_settings())
    print(
        f"Run {s.run_id}: {s.status}. Window {s.window_start:%Y-%m-%d %H:%M} to "
        f"{s.window_end:%Y-%m-%d %H:%M} UTC. {s.queries} queries ({s.query_errors} failed), "
        f"{s.fetched} fetched, {s.inserted} new."
    )
    return 0 if s.status == "ok" else 1


def cmd_retag(_: argparse.Namespace) -> int:
    print(f"Re-tagged {jobs.retag(get_settings())} articles.")
    return 0


def cmd_prune(_: argparse.Namespace) -> int:
    print(f"Removed {jobs.prune(get_settings())} articles past retention.")
    return 0


def cmd_config_check(_: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        outlets = load_outlets(settings.config_dir / "outlets.yaml")
        tags = load_tags(settings.config_dir / "tags.yaml")
    except ConfigError as exc:
        print(f"Invalid: {exc}")
        return 1
    print(f"OK: {len(outlets)} outlets, {len(tags)} tags.")
    return 0


def cmd_outlets_list(_: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        rows = conn.execute(
            "SELECT o.domain, o.display_name, o.country, o.language, o.active,"
            " count(a.id) AS articles, max(a.published_at) AS latest"
            " FROM outlets o LEFT JOIN articles a ON a.outlet_id = o.id"
            " GROUP BY o.id ORDER BY o.domain"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        state = "" if r["active"] else "  (inactive)"
        print(
            f"{r['domain']:<28} {r['country']} {r['language']}  {r['articles']:>6}  "
            f"{r['latest'] or '-':<20}  {r['display_name']}{state}"
        )
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
    sub.add_parser("ingest", help="fetch new articles now").set_defaults(func=cmd_ingest)
    sub.add_parser("retag", help="recompute tags after editing tags.yaml").set_defaults(
        func=cmd_retag
    )
    sub.add_parser("prune", help="delete articles past RETENTION_DAYS").set_defaults(func=cmd_prune)
    config = sub.add_parser("config", help="config file tools").add_subparsers(
        dest="config_cmd", required=True
    )
    config.add_parser("check", help="validate outlets.yaml and tags.yaml").set_defaults(
        func=cmd_config_check
    )
    outlets = sub.add_parser("outlets", help="outlet tools").add_subparsers(
        dest="outlets_cmd", required=True
    )
    outlets.add_parser("list", help="outlets with article counts").set_defaults(
        func=cmd_outlets_list
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
