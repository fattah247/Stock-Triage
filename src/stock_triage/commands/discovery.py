"""Risk discovery commands: watch-sync, themes, findings (docs/market_risk_plan.md, component 5)."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import httpx

from ..config import load_config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from ..logging_setup import setup_logging
from ..market.discovery import (
    _jakarta_today,
    active_themes,
    dismiss_finding,
    load_open_findings,
    load_theme_headlines,
    promote_finding,
    replay_findings,
    run_watch_sync,
)
from ..market.novelty import BASELINE_MIN_DAYS
from ..market.themes import ThemeFileError, load_themes
from ..market.watch import WatchSourceError, load_watch_sources


def cmd_watch_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    sources = {item.strip() for item in args.source.split(",")} if args.source else None
    try:
        result = run_watch_sync(
            cfg,
            source_ids=sources,
            since=date.fromisoformat(args.since) if args.since else None,
            use_llm=not args.no_llm,
        )
    except (ThemeFileError, WatchSourceError) as exc:
        print(f"watch-sync: {exc}")
        return 1
    print(
        "watch-sync: "
        f"sources={result.sources_fetched} items_seen={result.items_seen} items_new={result.items_new} "
        f"tagged_keyword={result.tagged_keyword} tagged_llm={result.tagged_llm} "
        f"findings_new={result.findings_new} findings_updated={result.findings_updated} anomalies={result.anomalies} "
        f"errors={len(result.errors)}"
    )
    if result.baseline_days < BASELINE_MIN_DAYS:
        print(
            f"  bursts and new pairings wait for {BASELINE_MIN_DAYS} days of headlines before this week "
            f"(have {result.baseline_days}); run watch-sync daily, or reach back with --source <gdelt source> --since"
        )
    if result.llm_error:
        print(f"  LLM tagging stopped: {result.llm_error}")
    for error in result.errors[:10]:
        print(f"  error: {error}")
    for theme in result.active_themes[:8]:
        print(f"  active theme: {theme['title']} ({theme['severity']}, {theme['items']} items in 3 days)")
    return 1 if result.errors and not result.sources_fetched else 0


def cmd_watch_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    try:
        sources = load_watch_sources(cfg.market.watch_sources_path)
    except WatchSourceError as exc:
        print(f"watch-status: {exc}")
        return 1
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        last_runs = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT r.* FROM watch_source_runs r
                WHERE r.fetched_at = (SELECT MAX(fetched_at) FROM watch_source_runs r2 WHERE r2.source_id = r.source_id)
                """
            )
        }
        totals = dict(conn.execute("SELECT source_id, COUNT(*) FROM watch_items GROUP BY source_id").fetchall())
        open_findings = conn.execute("SELECT kind, COUNT(*) FROM emerging_findings WHERE status = 'open' GROUP BY kind").fetchall()
    finally:
        conn.close()
    print(f"watch-status: sources={len(sources)} registry={cfg.market.watch_sources_path}")
    for source in sources:
        run = last_runs.get(source.source_id)
        state = "never fetched" if run is None else f"{run['status']} at {run['fetched_at'][:16]} ({run['item_count']} items, {run['new_count']} new)"
        flag = "" if source.enabled else " [disabled]"
        print(f"  {source.source_id:<24} {source.kind:<11} trust={source.trust:<6} stored={totals.get(source.source_id, 0):<5} {state}{flag}")
        if run is not None and run["error"]:
            print(f"    error: {run['error']}")
    print("  open findings: " + (", ".join(f"{kind}={count}" for kind, count in open_findings) or "none"))
    return 0


def cmd_themes(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        themes = load_themes(conn, cfg.market.risk_themes_path)
        as_of = date.fromisoformat(args.as_of) if args.as_of else _jakarta_today()
        active = active_themes(conn, themes=themes, as_of=as_of, min_items=cfg.market.theme_active_min_items)
        headlines = {item["theme_id"]: load_theme_headlines(conn, theme_id=str(item["theme_id"]), limit=3) for item in active}
    except ThemeFileError as exc:
        print(f"themes: {exc}")
        return 1
    finally:
        conn.close()
    print(f"themes: tracked={len(themes)} active={len(active)} as_of={as_of}")
    for item in active:
        print(f"  {item['severity']:<6} {item['title']} ({item['items']} items in 3 days)")
        for headline in headlines[item["theme_id"]]:
            print(f"      - {headline['title']} [{headline['source_id']}]")
    if args.all:
        active_ids = {item["theme_id"] for item in active}
        for theme in themes:
            if theme.theme_id not in active_ids:
                print(f"  {theme.severity:<6} {theme.title} [{theme.theme_id}, {theme.origin}]")
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        findings = load_open_findings(conn, limit=args.limit)
    finally:
        conn.close()
    if not findings:
        print("findings: none open")
        return 0
    for item in findings:
        details = item["details"]
        extra = details.get("note") or details.get("example") or details.get("title") or ""
        ratio = f" x{item['burst_ratio']:g}" if item["burst_ratio"] else ""
        print(f"{item['finding_id']}  {item['kind']:<12} '{item['term']}'{ratio}  items={item['item_count']} detected={item['detected_on']}")
        if extra:
            print(f"    {str(extra)[:200]}")
    return 0


def cmd_finding_promote(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    keywords = [item for item in args.keywords.split(",")]
    sectors = [item.strip() for item in (args.sectors or "").split(",") if item.strip()]
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        try:
            wq.submit(
                lambda conn: promote_finding(
                    conn,
                    finding_id=args.finding_id,
                    theme_id=args.theme_id,
                    title=args.title,
                    keywords=keywords,
                    severity=args.severity,
                    sectors=sectors,
                    category=args.category,
                    note=args.note,
                )
            )
        except ValueError as exc:
            print(f"finding-promote: {exc}")
            return 1
    print(f"finding-promote: {args.finding_id} is now theme {args.theme_id}; the next watch-sync tags recent items with it")
    return 0


def cmd_finding_dismiss(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        try:
            wq.submit(lambda conn: dismiss_finding(conn, finding_id=args.finding_id, note=args.note))
        except ValueError as exc:
            print(f"finding-dismiss: {exc}")
            return 1
    print(f"finding-dismiss: {args.finding_id} dismissed")
    return 0


def cmd_watch_replay(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    end = date.fromisoformat(args.to) if args.to else _jakarta_today()
    start = date.fromisoformat(args.from_date) if args.from_date else end - timedelta(days=30)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        hits = replay_findings(conn, start=start, end=end, match=args.match)
        first_item = None
        if args.match:
            row = conn.execute(
                "SELECT MIN(COALESCE(published_at, first_seen_at)) FROM watch_items WHERE LOWER(title) LIKE ?",
                (f"%{args.match.lower()}%",),
            ).fetchone()
            first_item = row[0] if row else None
    finally:
        conn.close()
    print(f"watch-replay: {start} to {end} findings={len(hits)} (nothing is stored)")
    if first_item:
        print(f"  first watched item containing '{args.match}': {first_item[:16]}")
    for hit in hits[: args.limit]:
        ratio = f" x{hit.burst_ratio:g}" if hit.burst_ratio else ""
        lag = (hit.detected_on - hit.first_seen).days
        print(f"  {hit.detected_on}  {hit.kind:<11} '{hit.term}'{ratio}  first seen {hit.first_seen} (+{lag}d)")
        if hit.example:
            print(f"      e.g. {hit.example[:150]}")
    return 0


def cmd_llm_eval(args: argparse.Namespace) -> int:
    from ..config.loader import REPO_ROOT
    from ..market.llm_eval import MAX_MEDIAN_SECONDS, evaluate_model, installed_chat_models, load_eval_cases, recommend
    from ..market.themes import LLMTagger, load_theme_file

    cfg = load_config()
    themes = load_theme_file(cfg.market.risk_themes_path)
    cases = load_eval_cases(Path(args.cases) if args.cases else REPO_ROOT / "config" / "theme_eval.yaml")
    try:
        models = [name.strip() for name in args.models.split(",")] if args.models else installed_chat_models(cfg.llm.base_url)
    except httpx.HTTPError as exc:
        print(f"llm-eval: cannot reach Ollama at {cfg.llm.base_url}: {exc}")
        return 1
    print(f"llm-eval: {len(cases)} headlines, {len(themes)} themes, models: {', '.join(models)}")
    scores = []
    for model in models:
        tagger = LLMTagger(base_url=cfg.llm.base_url, model_name=model, timeout_s=args.timeout)
        score = evaluate_model(tagger, cases, themes)
        scores.append(score)
        print(
            f"  {model:<24} accuracy={score.accuracy:.0%} recall={score.recall:.0%} precision={score.precision:.0%} "
            f"f1={score.f1:.2f} evidence_failures={score.evidence_failures} errors={score.errors} "
            f"median={score.median_seconds:.1f}s p90={score.p90_seconds:.1f}s"
        )
        if args.show_misses:
            for title, expected, got in score.misses:
                print(f"      expected {expected or 'none'}, got {got or 'none'}: {title[:80]}")
    best = recommend(scores)
    if best is None:
        print("  no model answered; is Ollama running?")
        return 1
    note = "" if best.fast_enough else f" (slower than the {MAX_MEDIAN_SECONDS:.1f}s per headline watch-sync's budget allows)"
    print(f"  best for theme tagging: {best.model}{note}; set llm.model_name to it in your config")
    return 0


def register(sub) -> None:
    p_sync = sub.add_parser(
        "watch-sync",
        help="Fetch watched news and official sources, tag risk themes, and raise emerging findings",
    )
    p_sync.add_argument("--source", help="Comma-separated source ids (default: every enabled source)")
    p_sync.add_argument("--since", help="For GDELT sources, also fetch back to this day, YYYY-MM-DD (about 3 months at most)")
    p_sync.add_argument("--no-llm", action="store_true", help="Tag with keywords only")
    p_sync.set_defaults(func=cmd_watch_sync)

    p_status = sub.add_parser("watch-status", help="Show each watched source's last fetch and the open findings")
    p_status.set_defaults(func=cmd_watch_status)

    p_themes = sub.add_parser("themes", help="List active risk themes with recent headlines")
    p_themes.add_argument("--as-of", help="Day to look at, YYYY-MM-DD (default today in Jakarta)")
    p_themes.add_argument("--all", action="store_true", help="Also list inactive themes")
    p_themes.set_defaults(func=cmd_themes)

    p_findings = sub.add_parser("findings", help="List open emerging findings")
    p_findings.add_argument("--limit", type=int, default=30)
    p_findings.set_defaults(func=cmd_findings)

    p_promote = sub.add_parser("finding-promote", help="Promote a finding to a tracked risk theme")
    p_promote.add_argument("finding_id")
    p_promote.add_argument("--theme-id", required=True, help="New theme id, e.g. msci_standalone")
    p_promote.add_argument("--title", required=True)
    p_promote.add_argument("--keywords", required=True, help="Comma-separated keywords (Indonesian and English)")
    p_promote.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    p_promote.add_argument("--sectors", help="Comma-separated IDX-IC sector index codes, e.g. IDXFINANCE,IDXBASIC")
    p_promote.add_argument("--category", help="Catalog category letter A-M")
    p_promote.add_argument("--note")
    p_promote.set_defaults(func=cmd_finding_promote)

    p_dismiss = sub.add_parser("finding-dismiss", help="Dismiss an emerging finding")
    p_dismiss.add_argument("finding_id")
    p_dismiss.add_argument("--note")
    p_dismiss.set_defaults(func=cmd_finding_dismiss)

    p_replay = sub.add_parser(
        "watch-replay",
        help="Show what the novelty detectors would have raised each day over stored items (read-only)",
    )
    p_replay.add_argument("--from", dest="from_date", help="First day, YYYY-MM-DD (default 30 days back)")
    p_replay.add_argument("--to", help="Last day, YYYY-MM-DD (default today)")
    p_replay.add_argument("--match", help="Only findings whose term contains this text, e.g. standalone")
    p_replay.add_argument("--limit", type=int, default=40)
    p_replay.set_defaults(func=cmd_watch_replay)

    p_eval = sub.add_parser(
        "llm-eval",
        help="Compare local Ollama models on labelled headlines (config/theme_eval.yaml) and recommend one",
    )
    p_eval.add_argument("--models", help="Comma-separated model names (default: every installed chat model)")
    p_eval.add_argument("--cases", help="Another labelled headline file")
    p_eval.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for one answer (default 120)")
    p_eval.add_argument("--show-misses", action="store_true", help="List each headline a model got wrong")
    p_eval.set_defaults(func=cmd_llm_eval)
