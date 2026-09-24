"""Command-line interface: `newsroom <command>`.

Run inside the worker container, e.g. `docker compose exec worker newsroom backup`.
Curation commands (outlets, ownership) are added in later phases.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from datetime import UTC, datetime, timedelta

from newsroom import __version__, funding_view, jobs
from newsroom.backup import backup
from newsroom.config import ConfigError, load_curated_funding, load_outlets, load_tags
from newsroom.db import connect
from newsroom.log import setup_logging
from newsroom.migrate import migrate
from newsroom.net.http import ApiError
from newsroom.ownership_view import Graph
from newsroom.services import funding, ownership
from newsroom.services.ingest import IngestBusy
from newsroom.settings import get_settings
from newsroom.sources.wikidata import WikidataSource


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
        curated = load_curated_funding(settings.config_dir / "public_funding.yaml")
    except ConfigError as exc:
        print(f"Invalid: {exc}")
        return 1
    print(f"OK: {len(outlets)} outlets, {len(tags)} tags, {len(curated)} curated funding records.")
    return 0


def cmd_outlets_list(_: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        rows = conn.execute(
            "SELECT o.domain, o.display_name, o.active, o.match_status, o.wikidata_qid,"
            " (SELECT count(*) FROM articles a WHERE a.outlet_id = o.id) AS articles"
            " FROM outlets o ORDER BY o.domain"
        ).fetchall()
    finally:
        conn.close()
    print(f"{'DOMAIN':<26} {'MATCH':<10} {'QID':<12} {'ARTICLES':>8}  NAME")
    for r in rows:
        state = "" if r["active"] else "  (inactive)"
        print(
            f"{r['domain']:<26} {r['match_status']:<10} {r['wikidata_qid'] or '-':<12} "
            f"{r['articles']:>8}  {r['display_name']}{state}"
        )
    return 0


def cmd_outlets_unmatched(_: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        rows = conn.execute(
            "SELECT id, domain, display_name, match_status FROM outlets"
            " WHERE active = 1 AND match_status IN ('unmatched', 'ambiguous') ORDER BY domain"
        ).fetchall()
        if not rows:
            print("Every active outlet has a Wikidata match (or a manual decision).")
        for r in rows:
            print(f"{r['domain']}  ({r['display_name']}): {r['match_status']}")
            for c in conn.execute(
                "SELECT qid, label, description FROM outlet_match_candidates"
                " WHERE outlet_id = ? ORDER BY qid",
                (r["id"],),
            ):
                print(f"    candidate {c['qid']:<12} {c['label']}  {c['description']}")
    finally:
        conn.close()
    print("\nFix with: newsroom outlets set-qid <domain> <QID>   (or 'none' if no item exists)")
    return 0


def cmd_outlets_set_qid(args: argparse.Namespace) -> int:
    settings = get_settings()
    qid = None if args.qid.lower() == "none" else args.qid.upper()
    conn = connect(settings.db_path)
    try:
        ownership.set_qid(conn, args.domain, qid, datetime.now(UTC))
    finally:
        conn.close()
    print(f"{args.domain} -> {qid or 'no Wikidata item'} (manual). Resolving ownership...")
    s = jobs.resolve_ownership(settings, [args.domain], rematch=False)
    print(f"Done: {s.entities} entities, {s.edges} ownership links.")
    return 0


def cmd_outlets_confirm(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        n = ownership.confirm(conn, args.domains or None)
    finally:
        conn.close()
    print(f"Confirmed {n} automatic match(es).")
    return 0


def cmd_ownership_resolve(args: argparse.Namespace) -> int:
    settings = get_settings()
    domains = args.domains or None
    if args.all:
        conn = connect(settings.db_path)
        try:
            domains = [r[0] for r in conn.execute("SELECT domain FROM outlets WHERE active = 1")]
        finally:
            conn.close()
    s = jobs.resolve_ownership(settings, domains)
    print(
        f"{s.outlets} outlet(s): {s.matched} matched, {s.ambiguous} ambiguous, "
        f"{s.unmatched} unmatched; {s.entities} entities, {s.edges} ownership links."
    )
    return 0


def cmd_ownership_show(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        outlet = conn.execute("SELECT * FROM outlets WHERE domain = ?", (args.domain,)).fetchone()
        if outlet is None:
            print(f"Unknown outlet: {args.domain}")
            return 1
        graph = Graph.load(conn)
    finally:
        conn.close()
    print(f"{outlet['display_name']} ({outlet['domain']})")
    print(f"  match: {outlet['match_status']} {outlet['wikidata_qid'] or ''}")
    node = graph.nodes.get(outlet["entity_id"]) if outlet["entity_id"] else None
    if node is None:
        print("  ownership: Not publicly disclosed (no Wikidata item)")
        return 0
    print(f"  item: {node.name} [{node.qid}]  {node.source_url}")

    def show(steps: list, depth: int) -> None:
        for step in steps:
            share = f" ({step.edge.share:.0%})" if step.edge.share else ""
            note = "  [cycle]" if step.cycle else ""
            print(
                f"{'  ' * depth}- {step.edge.label}: {step.parent.name} [{step.parent.qid}]"
                f"{share}{note}  source: {step.edge.source_url} ({step.edge.retrieved_at[:10]})"
            )
            show(step.above, depth + 1)

    chain = graph.chain(node.id)
    if chain:
        show(chain, 2)
    else:
        print("    Owner: Not publicly disclosed (no owner recorded on Wikidata)")
    return 0


def cmd_ownership_add_edge(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = jobs.wikidata_client(settings)
    conn = connect(settings.db_path)
    try:
        ownership.add_manual_edge(
            conn,
            WikidataSource(client),
            args.child.upper(),
            args.parent.upper(),
            args.relation,
            args.source_url,
            datetime.now(UTC),
        )
    finally:
        conn.close()
        client.close()
    print(f"Added manual edge {args.child} -> {args.parent} ({args.relation}).")
    return 0


def cmd_ownership_remove_edge(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        n = ownership.remove_manual_edge(conn, args.child.upper(), args.parent.upper())
    finally:
        conn.close()
    print(f"Removed {n} manual edge(s).")
    return 0


def cmd_funding_refresh(args: argparse.Namespace) -> int:
    s = jobs.refresh_funding(get_settings(), force=args.all)
    print(
        f"Identifiers checked: {s.checked} ({s.failed} failed), {s.records} records. "
        f"Curated: {s.curated} attached, {s.curated_skipped} skipped (outlet not resolved)."
    )
    return 0 if not s.failed else 1


def cmd_funding_show(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        outlet = conn.execute("SELECT * FROM outlets WHERE domain = ?", (args.domain,)).fetchone()
        if outlet is None:
            print(f"Unknown outlet: {args.domain}")
            return 1
        graph = Graph.load(conn)
        entity_ids = graph.lineage(outlet["entity_id"])
        rows = funding_view.records(conn, entity_ids)
    finally:
        conn.close()
    if not rows:
        print("Funding: Not publicly disclosed")
    for r in rows:
        amount = f"{r['amount']:,.0f} {r['currency']}" if r["amount"] is not None else "(link)"
        print(
            f"{graph.nodes[r['entity_id']].name}: {r['label']}: {amount}"
            f"{' · ' + r['period'] if r['period'] else ''}\n    source: {r['source_url']}"
            f" (retrieved {r['retrieved_at'][:10]})"
        )
    return 0


def cmd_entities_set_id(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        value = funding.set_identifier(
            conn, args.qid.upper(), args.scheme, args.value, datetime.now(UTC)
        )
    finally:
        conn.close()
    print(f"{args.qid}: {args.scheme} = {value}. Looked up on the next funding run (make funding).")
    return 0


def cmd_entities_remove_id(args: argparse.Namespace) -> int:
    conn = connect(get_settings().db_path)
    try:
        n = funding.remove_identifier(conn, args.qid.upper(), args.scheme, args.value)
    finally:
        conn.close()
    print(f"Removed {n} manual identifier(s).")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    """One-screen overview for routine checks."""
    settings = get_settings()
    if not settings.db_path.exists():
        print("No database yet (the worker creates it on start).")
        return 1
    now = datetime.now(UTC)
    conn = connect(settings.db_path)
    try:
        one = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
        run = one(
            "SELECT status, started_at, finished_at, queries, inserted, query_errors"
            " FROM ingest_runs"
            " ORDER BY id DESC LIMIT 1"
        )
        ok = one("SELECT max(finished_at) FROM ingest_runs WHERE status = 'ok'")[0]
        arts = one("SELECT count(*), max(published_at) FROM articles")
        # "Up to date" = fetched through a recent run (two intervals, to allow for a run
        # in progress). Outlets never fetched without error have no cursor at all.
        fresh_after = (now - timedelta(minutes=2 * settings.ingest_interval_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        behind = [
            r[0]
            for r in conn.execute(
                "SELECT o.domain FROM outlets o LEFT JOIN ingest_cursors c"
                " ON c.domain = o.domain AND c.source = 'gdelt'"
                " WHERE o.active = 1 AND (c.window_end IS NULL OR c.window_end < ?)"
                " ORDER BY o.domain",
                (fresh_after,),
            )
        ]
        active = one("SELECT count(*) FROM outlets WHERE active = 1")[0]
        recent_after = (now - timedelta(days=settings.pubdate_max_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        pub = conn.execute(
            "SELECT count(*), count(outlet_published_at),"
            " sum(outlet_published_at IS NULL AND pubdate_attempts >= 2)"
            " FROM articles WHERE published_at >= ?",
            (recent_after,),
        ).fetchone()
        matches = dict(
            conn.execute(
                "SELECT match_status, count(*) FROM outlets WHERE active = 1 GROUP BY 1"
            ).fetchall()
        )
        checked = one("SELECT min(ownership_checked_at) FROM outlets WHERE active = 1")[0]
        ents = one("SELECT count(*) FROM entities")[0]
        edges = one("SELECT count(*) FROM ownership_edges")[0]
        funds = dict(
            conn.execute("SELECT source, count(*) FROM funding_records GROUP BY 1").fetchall()
        )
    finally:
        conn.close()
    backups = sorted(settings.backup_dir.glob("newsroom-*.sqlite3"))
    print("Ingestion")
    if run:
        if run["status"] == "running":
            print(
                f"  current run:     running since {run['started_at']}: {run['queries']}"
                f" requests done ({run['query_errors']} failed), {run['inserted']} new so far"
            )
        else:
            print(
                f"  last run:        {run['status']} at {run['finished_at']},"
                f" {run['inserted']} new, {run['query_errors']} failed queries"
            )
    print(f"  last full run:   {ok or 'never'}")
    print(f"  articles:        {arts[0]} (newest {arts[1] or '-'})")
    print(f"  outlets current: {active - len(behind)} of {active}")
    print(
        f"  pub. dates:      {pub[1]} of {pub[0]} recent articles"
        f" ({pub[2] or 0} pages give none; the rest show GDELT's 'Seen' time)"
    )
    if behind:
        shown = ", ".join(behind[:6]) + (f" and {len(behind) - 6} more" if len(behind) > 6 else "")
        print(f"  behind:          {shown}")
    print("Ownership")
    print("  outlet matches:  " + ", ".join(f"{k} {v}" for k, v in sorted(matches.items())))
    print(f"  oldest check:    {checked or 'never'}")
    print(f"  entities/links:  {ents} / {edges}")
    print("Funding")
    print(
        "  records:         " + (", ".join(f"{k} {v}" for k, v in sorted(funds.items())) or "none")
    )
    print("Backups")
    if backups:
        age = now.timestamp() - backups[-1].stat().st_mtime
        print(
            f"  latest:          {backups[-1].name} ({age / 3600:.1f} h ago), {len(backups)} kept"
        )
    else:
        print("  latest:          none yet (nightly at BACKUP_HOUR, or: newsroom backup)")
    if not settings.contact_email:
        print("\nWarning: CONTACT_EMAIL is not set; ownership and funding lookups are disabled.")
    return 0


def cmd_pubdates(args: argparse.Namespace) -> int:
    s = jobs.publication_dates(get_settings(), args.limit)
    print(
        f"Pages read: {s.checked}. Dates found: {s.found}; no date on page: {s.no_date};"
        f" robots.txt disallows: {s.robots_disallowed}; failed: {s.failed}"
        f" ({s.hosts_skipped} site(s) left alone for this run)."
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
    sub.add_parser("status", help="overview: ingestion, ownership, funding, backups").set_defaults(
        func=cmd_status
    )
    sub.add_parser("ingest", help="fetch new articles now").set_defaults(func=cmd_ingest)
    pub = sub.add_parser("pubdates", help="read publication dates from recent article pages now")
    pub.add_argument("--limit", type=int, default=None, help="pages to read (default 150)")
    pub.set_defaults(func=cmd_pubdates)
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
    outlets.add_parser("list", help="outlets, match status, article counts").set_defaults(
        func=cmd_outlets_list
    )
    outlets.add_parser(
        "unmatched", help="outlets without a Wikidata match, with candidates"
    ).set_defaults(func=cmd_outlets_unmatched)
    set_qid = outlets.add_parser("set-qid", help="pin an outlet to a Wikidata item")
    set_qid.add_argument("domain")
    set_qid.add_argument("qid", help="e.g. Q12345, or 'none' if no item exists")
    set_qid.set_defaults(func=cmd_outlets_set_qid)
    conf = outlets.add_parser("confirm", help="confirm automatic matches (all if none given)")
    conf.add_argument("domains", nargs="*")
    conf.set_defaults(func=cmd_outlets_confirm)

    own = sub.add_parser("ownership", help="ownership tools").add_subparsers(
        dest="ownership_cmd", required=True
    )
    resolve = own.add_parser(
        "resolve", help="re-run ownership resolution (due outlets, given domains, or --all)"
    )
    resolve.add_argument("domains", nargs="*")
    resolve.add_argument("--all", action="store_true", help="every active outlet, now")
    resolve.set_defaults(func=cmd_ownership_resolve)
    show = own.add_parser("show", help="print an outlet's ownership chain with sources")
    show.add_argument("domain")
    show.set_defaults(func=cmd_ownership_show)
    add = own.add_parser("add-edge", help="record an ownership link missing from Wikidata")
    add.add_argument("child", help="QID of the owned entity")
    add.add_argument("parent", help="QID of the owner")
    add.add_argument("--relation", choices=["owned_by", "parent_org"], default="owned_by")
    add.add_argument("--source-url", required=True, help="public record supporting this link")
    add.set_defaults(func=cmd_ownership_add_edge)
    rm = own.add_parser("remove-edge", help="remove a manual ownership link")
    rm.add_argument("child")
    rm.add_argument("parent")
    rm.set_defaults(func=cmd_ownership_remove_edge)
    fund = sub.add_parser("funding", help="funding tools").add_subparsers(
        dest="funding_cmd", required=True
    )
    refresh = fund.add_parser("refresh", help="look up funding records that are due")
    refresh.add_argument("--all", action="store_true", help="re-check every identifier now")
    refresh.set_defaults(func=cmd_funding_refresh)
    fshow = fund.add_parser("show", help="print funding records for an outlet and its owners")
    fshow.add_argument("domain")
    fshow.set_defaults(func=cmd_funding_show)

    ent = sub.add_parser("entities", help="identifier tools").add_subparsers(
        dest="entities_cmd", required=True
    )
    for name, func, text in (
        ("set-id", cmd_entities_set_id, "record a registry identifier for an entity"),
        ("remove-id", cmd_entities_remove_id, "remove a manually recorded identifier"),
    ):
        p = ent.add_parser(name, help=text)
        p.add_argument("qid")
        p.add_argument("scheme", choices=["sec_cik", "us_ein", "ca_bn"])
        p.add_argument("value", help="e.g. 0000123456, 12-3456789, 123456789RR0001")
        p.set_defaults(func=func)

    health = sub.add_parser("healthcheck", help="exit 0 if healthy (container healthcheck)")
    health.add_argument("target", choices=["web", "worker"])
    health.set_defaults(func=cmd_healthcheck)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command != "healthcheck":
        setup_logging(get_settings().log_level)
    try:
        code = args.func(args)
    except (ConfigError, LookupError, ValueError, IngestBusy, ApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
