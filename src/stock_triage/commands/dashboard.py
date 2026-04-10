"""Dashboard export and serve commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_config
from ..dashboard import export_dashboard, serve_dashboard
from ..logging_setup import setup_logging


def cmd_dashboard_export(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = export_dashboard(
        cfg,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(
        "dashboard-export: "
        f"html={result.html_path} "
        f"metrics_csv={result.metrics_csv_path} "
        f"findings_csv={result.findings_csv_path} "
        f"issuers={result.issuer_count} "
        f"documents={result.document_count} "
        f"findings={result.finding_count}"
    )
    return 0


def cmd_dashboard_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    host = args.host or cfg.dashboard.host
    port = args.port or cfg.dashboard.port
    print(f"dashboard-serve: url=http://{host}:{port}")
    serve_dashboard(
        cfg,
        host=host,
        port=port,
        open_browser=args.open_browser,
    )
    return 0


def register(sub) -> None:
    p_dashboard_export = sub.add_parser(
        "dashboard-export",
        help="Render a local HTML dashboard and CSV exports",
    )
    p_dashboard_export.add_argument("--output-dir", help="Optional output directory for dashboard artifacts")
    p_dashboard_export.set_defaults(func=cmd_dashboard_export)

    p_dashboard_serve = sub.add_parser(
        "dashboard-serve",
        help="Serve the local dashboard with action buttons",
    )
    p_dashboard_serve.add_argument("--host", help="Override dashboard bind host")
    p_dashboard_serve.add_argument("--port", type=int, help="Override dashboard bind port")
    p_dashboard_serve.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the dashboard in the default browser after starting the server",
    )
    p_dashboard_serve.set_defaults(func=cmd_dashboard_serve)
