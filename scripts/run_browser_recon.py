#!/usr/bin/env python
"""Run the browser-backed Phase 1 proof-of-access flow for IDX surfaces.

Usage:
    python scripts/run_browser_recon.py [--headless]

Outputs:
    data/recon/browser/<source>/page.html
    data/recon/browser/<source>/responses/*
    data/recon/browser/<source>/storage_state.json
    data/recon/browser_findings.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_triage.config import load_config  # noqa: E402
from stock_triage.recon import (  # noqa: E402
    BrowserBlocked,
    BrowserFinding,
    PlaywrightSession,
    idx_source_surfaces,
    write_browser_findings,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Use headless mode. Useful for automated checks, but likely to stay blocked.",
    )
    args = parser.parse_args()

    cfg = load_config()
    cfg.ensure_dirs()
    session = PlaywrightSession(
        profile_dir=cfg.browser.profile_dir,
        artifacts_dir=cfg.browser.artifacts_dir,
        headless=args.headless if args.headless else cfg.browser.headless,
        challenge_timeout_s=cfg.browser.challenge_timeout_s,
        page_settle_ms=cfg.browser.page_settle_ms,
        channel=cfg.browser.channel,
    )

    findings = []
    for source in idx_source_surfaces():
        print(f"Probing {source['id']} -> {source['url']}")
        try:
            capture = session.capture(
                source_id=source["id"],
                source_class=source["source_class"],
                url=source["url"],
            )
        except BrowserBlocked as exc:
            print(f"  blocked: {exc}")
            findings.append(
                BrowserFinding(
                    source_id=source["id"],
                    source_class=source["source_class"],
                    url=source["url"],
                    accessible=False,
                    blocked=True,
                    error=str(exc),
                )
            )
            continue
        findings.append(
            BrowserFinding(
                source_id=source["id"],
                source_class=source["source_class"],
                url=source["url"],
                accessible=capture.accessible,
                blocked=capture.blocked,
                capture=capture,
            )
        )
        print(
            f"  accessible={capture.accessible} "
            f"responses={len(capture.responses)} "
            f"cookies={','.join(capture.cookies) or '-'}"
        )

    out_path = cfg.paths.data_dir / "recon" / "browser_findings.json"
    write_browser_findings(out_path, findings)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
