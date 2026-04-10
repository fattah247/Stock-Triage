"""CLI entrypoint: `python -m stock_triage <command>`.

Command implementations live under ``stock_triage.commands``; this module is
intentionally a thin dispatcher so that new verbs only touch one file.
"""

from __future__ import annotations

import argparse

from .commands import register_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock_triage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_all(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
