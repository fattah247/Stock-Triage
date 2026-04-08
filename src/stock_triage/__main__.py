"""CLI entrypoint: `python -m stock_triage <command>`."""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import WriterQueue, run_migrations
from .logging_setup import get_logger, setup_logging


def cmd_init(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    log = get_logger("stock_triage.init")
    log.info("ensuring directories", extra={"data_dir": str(cfg.paths.data_dir)})
    with WriterQueue(cfg) as wq:
        applied = wq.submit(run_migrations)
    log.info("migrations applied", extra={"versions": applied, "db": str(cfg.paths.db_path)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock_triage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="Create directories and apply DB migrations")
    p_init.set_defaults(func=cmd_init)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
