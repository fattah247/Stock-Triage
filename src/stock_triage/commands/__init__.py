"""CLI command registry.

Each submodule exposes a ``register(sub)`` function that adds its parser(s)
to the top-level ``argparse`` subparser. ``register_all`` walks the registry
in a deterministic order so ``python -m stock_triage <command>`` keeps a
stable surface.
"""

from __future__ import annotations

from argparse import _SubParsersAction

from . import (
    alerts_canary,
    coverage as coverage_cmd,
    dashboard as dashboard_cmd,
    hardening as hardening_cmd,
    intelligence as intelligence_cmd,
    pipeline,
    summary as summary_cmd,
)

_MODULES = (
    pipeline,
    summary_cmd,
    coverage_cmd,
    dashboard_cmd,
    alerts_canary,
    hardening_cmd,
    intelligence_cmd,
)


def register_all(sub: _SubParsersAction) -> None:
    for module in _MODULES:
        module.register(sub)


__all__ = ["register_all"]
