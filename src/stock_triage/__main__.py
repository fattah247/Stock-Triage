"""CLI entrypoint: `python -m stock_triage <command>`.

Command implementations live under ``stock_triage.commands``; this module is
intentionally a thin dispatcher so that new verbs only touch one file.
"""

from __future__ import annotations

import argparse
import sys

from .commands import register_all
from .config import load_config
from .runlock import (
    EXIT_PIPELINE_BUSY,
    PIPELINE_COMMANDS,
    UNLOCKED_COMMANDS,
    PipelineBusyError,
    lock_path_for,
    pipeline_lock,
)

__all__ = ["PIPELINE_COMMANDS", "UNLOCKED_COMMANDS", "build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock_triage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_all(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd not in PIPELINE_COMMANDS:
        return args.func(args)
    cfg = load_config()
    command_line = " ".join(argv if argv is not None else sys.argv[1:])
    try:
        with pipeline_lock(lock_path_for(cfg.paths.data_dir), command=command_line):
            return args.func(args)
    except PipelineBusyError as exc:
        print(f"{args.cmd}: {exc}", file=sys.stderr)
        return EXIT_PIPELINE_BUSY


if __name__ == "__main__":
    raise SystemExit(main())
