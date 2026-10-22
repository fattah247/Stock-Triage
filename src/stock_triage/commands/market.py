"""Market risk commands: risk calendar and exposure ranking (docs/market_risk_plan.md)."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from ..alerts import run_alerts
from ..config import load_config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from ..logging_setup import setup_logging
from ..market import (
    CatalystFileError,
    load_upcoming_catalysts,
    run_exposure,
    run_market_signals,
    run_market_sync,
    sync_catalysts,
)
from ..market.backtest import run_market_backtest, write_report
from ..market.forecast import MODEL_VERSION, load_episodes, record_episode_note, run_crash_risk
from ..market.sync import MAX_CONSECUTIVE_FAILURES


def cmd_catalysts_sync(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    try:
        result = sync_catalysts(cfg)
    except CatalystFileError as exc:
        print(f"catalysts-sync: error: {exc}")
        return 1
    if not result.file_found:
        print(
            f"catalysts-sync: no file at {result.path}; copy config/catalysts.example.yaml there to start a risk calendar"
        )
        return 0
    print(f"catalysts-sync: synced={result.synced} path={result.path}")
    return 0


def cmd_risk_calendar(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    today = date.fromisoformat(args.from_date) if args.from_date else datetime.now(timezone.utc).date()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        upcoming = load_upcoming_catalysts(
            conn,
            today=today,
            days=args.days,
            watchlist={ticker.upper() for ticker in cfg.alerts.watchlist},
        )
    finally:
        conn.close()
    if not upcoming:
        print(f"risk-calendar: nothing in the {args.days} days from {today.isoformat()}")
        return 0
    for catalyst in upcoming:
        print(f"{catalyst.catalyst_date.isoformat()}  {catalyst.severity:<6}  {catalyst.kind:<18}  {catalyst.title}")
    return 0


def cmd_exposure_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_exposure(cfg, as_of=date.fromisoformat(args.as_of) if args.as_of else None)
    market_rows = [row for row in result.breadth if row.scope == "market"]
    print(f"exposure-run: as_of={result.as_of.isoformat()} scored={len(result.scores)} breadth_rows={len(result.breadth)}")
    for score in result.scores[: args.top]:
        reasons = "; ".join(component.reason for component in score.components if component.points)
        print(f"  {score.ticker:<6} {score.score:>5.0f}  {score.period or '-':<8}  {reasons or 'no fragility found'}")
    for row in market_rows[-3:]:
        change = f"{row.change_pts:+.1f} pts" if row.change_pts is not None else "n/a"
        matched = max(row.details.get("matched_filers", {}).values(), default=0)
        print(
            f"  breadth FY{row.fiscal_year}: filers={row.filer_count} matched={matched} change={change} level={row.level}"
        )
    return 0


def cmd_market_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_market_sync(
        cfg,
        start=date.fromisoformat(args.backfill_from) if args.backfill_from else None,
        end=date.fromisoformat(args.to) if args.to else None,
        refresh=args.refresh,
    )
    print(
        "market-sync: "
        f"from={result.start.isoformat()} to={result.end.isoformat()} "
        f"idx_days={result.idx_days_fetched} no_data_days={result.idx_days_no_data} "
        f"already_synced={result.idx_days_skipped} jisdor_rates={result.jisdor_rates} "
        f"notices_new={result.notices_new} errors={len(result.errors)}"
    )
    for error in result.errors[:10]:
        print(f"  error: {error}")
    if result.idx_days_left:
        print(
            f"  stopped after {MAX_CONSECUTIVE_FAILURES} failed days in a row; "
            f"{result.idx_days_left} days left for the next run"
        )
        return 1
    # Some days fetched means progress was kept; only total failure is a failure.
    return 1 if result.errors and not (result.idx_days_fetched or result.jisdor_rates) else 0


def cmd_market_signals(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_market_signals(cfg)
    if result.latest is None:
        print("market-signals: no market data yet; run market-sync first")
        return 0
    latest = result.latest
    print(f"market-signals: days={result.days} latest={latest.trade_date.isoformat()} state={latest.regime}")
    for reading in latest.readings:
        print(f"  {reading.signal_name:<20} {reading.level:<9} {reading.text or '-'}")
    for event in latest.event_risk:
        print(f"  event risk: {event['date']} {event['title']}")
    return 0


def cmd_crash_risk(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_crash_risk(cfg)
    latest = result.latest
    if latest is None:
        print("crash-risk: no market data yet; run market-sync and market-signals first")
        return 0
    record = result.record
    market = cfg.market
    print(
        f"crash-risk: days={len(result.days)} latest={latest.trade_date.isoformat()} "
        f"episodes={len(result.episodes)} model={MODEL_VERSION}"
    )
    print(
        f"  a crash = a fall of {market.crash_risk_drawdown_pct:g}% within {market.crash_risk_horizon_sessions} sessions"
    )
    print(
        f"  track record: scored_days={record.scored_days} crash_days={record.crash_days} "
        f"brier={_fmt(record.brier)} base_rate_brier={_fmt(record.base_rate_brier)} skill={_pct(record.skill)} "
        f"hit_rate={_pct(record.hit_rate)} false_alarms={_pct(record.false_alarm_rate)}"
    )
    if latest.probability is None:
        print("  estimate: not enough history to fit yet")
    elif latest.trustworthy:
        print(f"  estimate: {latest.probability:.0%} (base rate {_pct(latest.base_rate)})")
    else:
        print(
            f"  estimate: withheld; it does not beat the base rate by {market.crash_risk_min_skill:.0%} "
            f"(model says {latest.probability:.0%}, base rate {_pct(latest.base_rate)})"
        )
    for analog in latest.analogs:
        recover = analog["sessions_to_recover"]
        print(
            f"  analog {analog['opened_on']}: worst {analog['worst_regime']}, fell a further {_fmt(analog['further_fall_pct'], 1)}% "
            f"over {analog['sessions_to_trough']} sessions, "
            + ("not recovered" if recover is None else f"recovered {recover} sessions after the bottom")
        )
    for evaluation in result.theses:
        print(f"  thesis {evaluation.thesis_id}: {evaluation.status}")
    return 0


def cmd_market_backtest(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = run_market_backtest(
        cfg,
        start=date.fromisoformat(args.from_date) if args.from_date else None,
        end=date.fromisoformat(args.to) if args.to else None,
    )
    if not report.days:
        print("market-backtest: no market data in range; run market-sync first")
        return 0
    path = write_report(report, Path(args.report) if args.report else cfg.paths.data_dir / "reports" / "market_backtest.md")
    record = report.risk.record
    print(f"market-backtest: {report.start} to {report.end} days={report.days} episodes={len(report.episodes)} report={path}")
    print("  states: " + " ".join(f"{regime}={count}" for regime, count in report.regime_counts.items()))
    for result in report.golden:
        outcome = "no data" if result.passed is None else ("pass" if result.passed else "FAIL")
        print(f"  golden {result.golden.day}: {outcome} (state {result.regime or '-'}, expected {result.expectation})")
    print(
        f"  estimate: brier={_fmt(record.brier)} base_rate_brier={_fmt(record.base_rate_brier)} skill={_pct(record.skill)} "
        f"hit_rate={_pct(record.hit_rate)} false_alarms={_pct(record.false_alarm_rate)}"
    )
    for item in report.episodes:
        episode = item.episode
        print(
            f"  episode {episode.opened_on}: worst={episode.worst_regime} max_drawdown={_fmt(episode.max_drawdown_pct, 1)}% "
            f"trough={episode.trough_date} state_lead={item.state_lead if item.state_lead is not None else '-'} "
            f"false_alarm={'yes' if item.false_alarm else 'no'}"
        )
    print(f"  crash stretches the estimate did not warn of: {len(report.missed_crashes)}")
    failed = [result for result in report.golden if result.passed is False]
    return 1 if failed else 0


def cmd_episode_note(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    themes = [item.strip() for item in args.themes.split(",") if item.strip()] if args.themes is not None else None
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        try:
            wq.submit(
                lambda conn: record_episode_note(
                    conn, episode_id=args.episode_id, cause=args.cause, lessons=args.lessons, theme_ids=themes
                )
            )
        except ValueError as exc:
            print(f"episode-note: {exc}")
            return 1
    print(f"episode-note: saved notes on {args.episode_id}")
    return 0


def cmd_episodes(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        episodes = load_episodes(conn)
    finally:
        conn.close()
    if not episodes:
        print("episodes: none yet; run crash-risk after a market backfill")
        return 0
    for item in episodes:
        print(
            f"{item['episode_id']}  {item['opened_on']} to {item['closed_on'] or 'open'}  worst={item['worst_regime']} "
            f"max_drawdown={_fmt(item['max_drawdown_pct'], 1)}% trough={item['trough_date']} "
            f"recovered={item['recovered_on'] or '-'}"
            + (f"\n    cause: {item['cause']}" if item["cause"] else "")
            + (f"\n    lessons: {item['lessons']}" if item["lessons"] else "")
        )
    return 0


def cmd_market_daily(args: argparse.Namespace) -> int:
    """The trading-day chain: sync, signals, crash risk, then alerts."""
    status = cmd_market_sync(args)
    if status and not args.continue_on_sync_error:
        print("market-daily: market-sync failed; stopping (pass --continue-on-sync-error to go on)")
        return status
    cmd_market_signals(args)
    cmd_crash_risk(args)
    cfg = load_config()
    result = run_alerts(cfg)
    print(f"alerts-run: selected={result.selected} emitted={result.emitted} skipped_existing={result.skipped_existing}")
    return 0


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def register(sub) -> None:
    p_sync = sub.add_parser(
        "catalysts-sync",
        help="Load the risk calendar file (market.catalysts_path) into the database",
    )
    p_sync.set_defaults(func=cmd_catalysts_sync)

    p_calendar = sub.add_parser(
        "risk-calendar",
        help="List upcoming risk dates, including watchlist filing deadlines",
    )
    p_calendar.add_argument("--days", type=int, default=30, help="How many days ahead to list (default 30)")
    p_calendar.add_argument("--from-date", help="Start date YYYY-MM-DD (default today, UTC)")
    p_calendar.set_defaults(func=cmd_risk_calendar)

    p_exposure = sub.add_parser(
        "exposure-run",
        help="Rank companies by market fragility and measure stress breadth across filers",
    )
    p_exposure.add_argument("--as-of", help="Date to record the run under, YYYY-MM-DD (default today, UTC)")
    p_exposure.add_argument("--top", type=int, default=10, help="How many of the most exposed to print (default 10)")
    p_exposure.set_defaults(func=cmd_exposure_run)

    p_sync = sub.add_parser(
        "market-sync",
        help="Fetch daily IDX index and stock summaries and Bank Indonesia's JISDOR rate",
    )
    p_sync.add_argument("--backfill-from", help="First day to fetch, YYYY-MM-DD (default: the last 10 days)")
    p_sync.add_argument("--to", help="Last day to fetch, YYYY-MM-DD (default today, UTC)")
    p_sync.add_argument("--refresh", action="store_true", help="Fetch days again even if already synced")
    p_sync.set_defaults(func=cmd_market_sync)

    p_signals = sub.add_parser(
        "market-signals",
        help="Compute market signals and the daily market state from synced data",
    )
    p_signals.set_defaults(func=cmd_market_signals)

    p_risk = sub.add_parser(
        "crash-risk",
        help="Estimate the chance of a crash, with its walk-forward track record, episodes and analogs",
    )
    p_risk.set_defaults(func=cmd_crash_risk)

    p_backtest = sub.add_parser(
        "market-backtest",
        help="Replay the market state and the crash-risk estimate over history and write a report",
    )
    p_backtest.add_argument("--from", dest="from_date", help="First day of the report, YYYY-MM-DD (default: all history)")
    p_backtest.add_argument("--to", help="Last day, YYYY-MM-DD (default: the latest synced day)")
    p_backtest.add_argument("--report", help="Where to write the Markdown report (default <data_dir>/reports/market_backtest.md)")
    p_backtest.set_defaults(func=cmd_market_backtest)

    p_episodes = sub.add_parser("episodes", help="List market episodes with their notes")
    p_episodes.set_defaults(func=cmd_episodes)

    p_note = sub.add_parser("episode-note", help="Record the cause, lessons and themes of a market episode")
    p_note.add_argument("episode_id", help="For example episode-2026-01-28 (see the episodes command)")
    p_note.add_argument("--cause", help="What drove it")
    p_note.add_argument("--lessons", help="What to do differently next time")
    p_note.add_argument("--themes", help="Comma-separated theme ids involved")
    p_note.set_defaults(func=cmd_episode_note)

    p_daily = sub.add_parser(
        "market-daily",
        help="Run market-sync, market-signals, crash-risk and alerts-run in order (once per trading day)",
    )
    p_daily.add_argument("--backfill-from", help=argparse.SUPPRESS)
    p_daily.add_argument("--to", help=argparse.SUPPRESS)
    p_daily.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    p_daily.add_argument(
        "--continue-on-sync-error", action="store_true", help="Compute signals from what is stored even if the sync failed"
    )
    p_daily.set_defaults(func=cmd_market_daily)
