#!/usr/bin/env python
"""Run Phase 1 reconnaissance probes against IDX/OJK public surfaces.

Usage:
    python scripts/run_recon.py [--no-cache]

Outputs:
    data/recon/<host>/<hash>.{bin,meta.json}   raw responses (gitignored)
    data/recon/findings.json                   structured per-source findings
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_triage.config import load_config  # noqa: E402
from stock_triage.recon.probe import run_probes, write_findings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cache", action="store_true", help="Force re-fetch")
    args = parser.parse_args()

    cfg = load_config()
    cfg.ensure_dirs()
    cache_root = cfg.paths.data_dir / "recon"

    if args.no_cache:
        # Wipe cache by ignoring it via a fresh root.
        import shutil
        if cache_root.exists():
            shutil.rmtree(cache_root)

    findings = run_probes(
        cache_root=cache_root,
        user_agent=cfg.discovery.user_agent,
        rate_limit_per_second=cfg.discovery.rate_limit_per_second,
        timeout_s=cfg.discovery.request_timeout_s,
    )
    out = cache_root / "findings.json"
    write_findings(out, findings)

    # Brief stdout summary so the user sees what happened.
    print(f"Wrote {out}")
    print(f"{'ID':40s} {'STATUS':6s} {'RENDER':32s}  ERROR")
    for f in findings["findings"]:
        print(
            f"{f['id']:40s} "
            f"{str(f.get('status_code') or '-'):6s} "
            f"{f.get('likely_render', '-'):32s}  "
            f"{f.get('error') or ''}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
