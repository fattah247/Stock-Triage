"""``watch-sync``: watched sources to themes, activity and findings (component 5).

One run fetches every enabled source, tags new items, recomputes daily theme
activity, runs the novelty detectors, and puts each finding in the review
queue. A reviewer dismisses a finding or promotes it to a tracked theme
(``finding-promote``), and every promotion widens what the next run tags.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from ..logging_setup import get_logger
from .idx_market import FOREIGN_NET_SERIES
from .novelty import (
    BASELINE_MIN_DAYS,
    DETECTOR_VERSION,
    Anomaly,
    Finding,
    Headline,
    baseline_days,
    detect_anomalies,
    detect_bursts,
    detect_new_pairings,
    detect_unclassified,
    terms,
)
from .themes import SEVERITY_WEIGHT, LLMTagger, Theme, keyword_tags, load_themes, persist_tags
from .watch import (
    GDELT_PAUSE_S,
    GdeltThrottled,
    WatchSource,
    fetch_source,
    item_day,
    load_watch_sources,
    persist_items,
    record_source_run,
)

# Items this many days back are re-tagged each run, so a new or promoted theme
# also covers recent news.
RETAG_DAYS = 120
HEADLINE_DAYS = 7 * 10
# A finding about the same term within this many days updates the open one.
FINDING_MERGE_DAYS = 30
LLM_ITEMS_PER_RUN = 40
# A slow local model must not hold the run lock for long; items past the budget keep keyword tags only.
LLM_TIME_BUDGET_S = 180.0
ANOMALY_LOOKBACK_DAYS = 10
FINDING_ITEM_TYPE = "emerging_finding"
ANOMALY_ITEM_TYPE = "market_anomaly"
CATALOG_ITEM_TYPE = "catalog_review"

logger = get_logger(__name__)


@dataclass
class WatchSyncResult:
    sources_fetched: int = 0
    items_seen: int = 0
    items_new: int = 0
    tagged_keyword: int = 0
    tagged_llm: int = 0
    findings_new: int = 0
    findings_updated: int = 0
    anomalies: int = 0
    llm_error: str | None = None
    baseline_days: int = 0
    errors: list[str] = field(default_factory=list)
    active_themes: list[dict[str, object]] = field(default_factory=list)


def run_watch_sync(
    cfg: Config,
    *,
    source_ids: set[str] | None = None,
    since: date | None = None,
    use_llm: bool = True,
    client: httpx.Client | None = None,
    llm_tagger: LLMTagger | None = None,
    today: date | None = None,
) -> WatchSyncResult:
    cfg.ensure_dirs()
    today = today or _jakarta_today()
    result = WatchSyncResult()
    sources = [
        source
        for source in load_watch_sources(cfg.market.watch_sources_path)
        if source.enabled and (source_ids is None or source.source_id in source_ids)
    ]
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": cfg.discovery.user_agent},
        timeout=cfg.discovery.request_timeout_s,
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),
    )
    tagger = llm_tagger
    if tagger is None and use_llm and cfg.llm.model_name:
        tagger = LLMTagger(base_url=cfg.llm.base_url, model_name=cfg.llm.model_name, timeout_s=min(cfg.llm.timeout_s, 90))

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        try:
            new_ids = _fetch_all(wq, client, sources, since=since, result=result, pause_s=1.0 / max(cfg.discovery.rate_limit_per_second, 0.1))
        finally:
            if own_client:
                client.close()
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            themes = load_themes(reader, cfg.market.risk_themes_path)
            recent = _recent_items(reader, days=RETAG_DAYS, today=today)
        finally:
            reader.close()

        keyword_by_item = {row["item_id"]: keyword_tags(row["title"], row["excerpt"], themes) for row in recent}
        result.tagged_keyword = sum(1 for tags in keyword_by_item.values() if tags)
        llm_tags: dict[str, list] = {}
        if tagger is not None:
            candidates = [
                row for row in recent if row["item_id"] in new_ids and not keyword_by_item.get(row["item_id"]) and row["trust"] != "low"
            ][:LLM_ITEMS_PER_RUN]
            started = time.monotonic()
            for row in candidates:
                if time.monotonic() - started > LLM_TIME_BUDGET_S:
                    result.llm_error = f"time budget of {LLM_TIME_BUDGET_S:.0f}s reached; the remaining new items keep keyword tags only"
                    break
                try:
                    tag = tagger.tag(title=row["title"], excerpt=row["excerpt"], themes=themes)
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    result.llm_error = f"{type(exc).__name__}: {exc}"[:200]
                    logger.warning("watch-sync: LLM tagging stopped: %s", result.llm_error)
                    break
                if tag is not None:
                    llm_tags[row["item_id"]] = [tag]
            result.tagged_llm = len(llm_tags)

        def store_tags(conn: sqlite3.Connection) -> None:
            ids = [row["item_id"] for row in recent]
            # Keyword tags are recomputed from the current themes; LLM tags are kept.
            conn.executemany("DELETE FROM item_themes WHERE item_id = ? AND method = 'keyword'", [(item_id,) for item_id in ids])
            for item_id, tags in keyword_by_item.items():
                persist_tags(conn, item_id=item_id, tags=tags)
            for item_id, tags in llm_tags.items():
                persist_tags(conn, item_id=item_id, tags=tags)
            _recompute_activity(conn, themes=themes, today=today, days=RETAG_DAYS)

        wq.submit(store_tags)

        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            headlines = load_headlines(reader, today=today, days=HEADLINE_DAYS)
            result.baseline_days = baseline_days(headlines, as_of=today)
            findings = detect_bursts(headlines, as_of=today) + detect_new_pairings(headlines, as_of=today)
            findings += detect_unclassified(headlines, as_of=today)
            anomalies = _anomaly_findings(reader, cfg, themes=themes, today=today)
            result.anomalies = len(anomalies)
            findings += anomalies
            result.active_themes = active_themes(reader, themes=themes, as_of=today, min_items=cfg.market.theme_active_min_items)
        finally:
            reader.close()
        counts = wq.submit(lambda conn: _store_findings(conn, findings=findings, themes=themes, today=today))
        result.findings_new, result.findings_updated = counts
        wq.submit(lambda conn: _ensure_catalog_review(conn, themes=themes, today=today))
    return result


# --- fetching -----------------------------------------------------------------


def _fetch_all(
    wq: WriterQueue,
    client: httpx.Client,
    sources: list[WatchSource],
    *,
    since: date | None,
    result: WatchSyncResult,
    pause_s: float,
) -> set[str]:
    new_ids: set[str] = set()
    last_call: dict[str, float] = {}
    for source in sources:
        host = urlparse(source.url or "").netloc or source.kind
        wait = (GDELT_PAUSE_S if source.kind == "gdelt" else pause_s) - (time.monotonic() - last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        if since is not None and source.kind == "gdelt":
            new_ids.update(_backfill_gdelt(wq, client, source, since=since, result=result))
            last_call[host] = time.monotonic()
            continue
        try:
            items = fetch_source(client, source)
        except Exception as exc:  # noqa: BLE001 - one broken feed must not stop the others
            message = f"{type(exc).__name__}: {exc}"[:300]
            result.errors.append(f"{source.source_id}: {message}")
            wq.submit(lambda conn, s=source, m=message: record_source_run(conn, source_id=s.source_id, status="error", item_count=0, new_count=0, error=m))
            last_call[host] = time.monotonic()
            continue
        last_call[host] = time.monotonic()
        fresh = wq.submit(lambda conn, s=source, i=items: persist_items(conn, items=i, trust=s.trust))
        new_ids.update(fresh)
        result.sources_fetched += 1
        result.items_seen += len(items)
        result.items_new += len(fresh)
        wq.submit(
            lambda conn, s=source, n=len(items), f=len(fresh): record_source_run(
                conn, source_id=s.source_id, status="ok", item_count=n, new_count=f, error=None
            )
        )
    return new_ids


def _backfill_gdelt(
    wq: WriterQueue, client: httpx.Client, source: WatchSource, *, since: date, result: WatchSyncResult
) -> set[str]:
    """A past window a day at a time, each day stored as it arrives; refused days are skipped."""
    new_ids: set[str] = set()
    day = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)
    seen = skipped = 0
    while day < end:
        until = min(day + timedelta(days=1), end)
        try:
            items = fetch_source(client, source, start=day, end=until)
        except GdeltThrottled:
            skipped += 1
            items = []
        except httpx.HTTPError as exc:
            result.errors.append(f"{source.source_id} {day.date()}: {type(exc).__name__}: {exc}"[:300])
            skipped += 1
            items = []
        if items:
            new_ids.update(wq.submit(lambda conn, i=items: persist_items(conn, items=i, trust=source.trust)))
            seen += len(items)
        day = until
        if day < end:
            time.sleep(GDELT_PAUSE_S)
    result.sources_fetched += 1
    result.items_seen += seen
    result.items_new += len(new_ids)
    if skipped:
        result.errors.append(f"{source.source_id}: GDELT refused {skipped} days of the window; run again to fill them")
    wq.submit(
        lambda conn: record_source_run(
            conn, source_id=source.source_id, status="ok" if seen else "error", item_count=seen, new_count=len(new_ids),
            error=f"{skipped} days refused" if skipped else None,
        )
    )
    return new_ids


def _recent_items(conn: sqlite3.Connection, *, days: int, today: date) -> list[sqlite3.Row]:
    since = (today - timedelta(days=days)).isoformat()
    return conn.execute(
        """
        SELECT item_id, source_id, trust, title, excerpt, published_at, first_seen_at
        FROM watch_items
        WHERE COALESCE(published_at, first_seen_at) >= ?
        """,
        (since,),
    ).fetchall()


def load_headlines(conn: sqlite3.Connection, *, today: date, days: int) -> list[Headline]:
    since = (today - timedelta(days=days + 1)).isoformat()
    rows = conn.execute(
        """
        SELECT wi.item_id, wi.title, wi.trust, wi.published_at, wi.first_seen_at,
               EXISTS (SELECT 1 FROM item_themes it WHERE it.item_id = wi.item_id) AS tagged
        FROM watch_items wi
        WHERE COALESCE(wi.published_at, wi.first_seen_at) >= ?
        """,
        (since,),
    ).fetchall()
    return _dedupe_headlines(
        [
            Headline(
                item_id=row["item_id"],
                day=item_day(row["published_at"], row["first_seen_at"]),
                title=row["title"],
                trust=row["trust"],
                tagged=bool(row["tagged"]),
            )
            for row in rows
        ]
    )


def _dedupe_headlines(headlines: list[Headline]) -> list[Headline]:
    # The same story syndicated across outlets counts once per day.
    seen: set[tuple[date, str]] = set()
    out = []
    for headline in sorted(headlines, key=lambda item: (item.day, item.trust != "high", item.item_id)):
        key = (headline.day, re.sub(r"\W+", " ", headline.title.lower()).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(headline)
    return out


# --- activity -----------------------------------------------------------------


def _recompute_activity(conn: sqlite3.Connection, *, themes: list[Theme], today: date, days: int) -> None:
    severity = {theme.theme_id: theme.severity for theme in themes}
    since = today - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT DISTINCT it.theme_id, wi.item_id, wi.published_at, wi.first_seen_at
        FROM item_themes it JOIN watch_items wi ON wi.item_id = it.item_id
        WHERE COALESCE(wi.published_at, wi.first_seen_at) >= ?
        """,
        ((since - timedelta(days=1)).isoformat(),),
    ).fetchall()
    counts: Counter[tuple[str, date]] = Counter()
    for row in rows:
        day = item_day(row["published_at"], row["first_seen_at"])
        if day >= since:
            counts[(row["theme_id"], day)] += 1
    conn.execute("DELETE FROM theme_activity_daily WHERE day >= ?", (since.isoformat(),))
    conn.executemany(
        "INSERT INTO theme_activity_daily (theme_id, day, item_count, severity_weighted) VALUES (?, ?, ?, ?)",
        [
            (theme_id, day.isoformat(), count, count * SEVERITY_WEIGHT.get(severity.get(theme_id, "medium"), 2.0))
            for (theme_id, day), count in sorted(counts.items())
        ],
    )


def active_themes(
    conn: sqlite3.Connection, *, themes: list[Theme], as_of: date, min_items: int, window_days: int = 3
) -> list[dict[str, object]]:
    """Themes with at least ``min_items`` items over the ``window_days`` up to ``as_of``."""
    by_id = {theme.theme_id: theme for theme in themes}
    rows = conn.execute(
        """
        SELECT theme_id, SUM(item_count) AS items
        FROM theme_activity_daily
        WHERE day > ? AND day <= ?
        GROUP BY theme_id
        HAVING SUM(item_count) >= ?
        ORDER BY items DESC
        """,
        ((as_of - timedelta(days=window_days)).isoformat(), as_of.isoformat(), min_items),
    ).fetchall()
    out = []
    for row in rows:
        theme = by_id.get(row["theme_id"])
        if theme is None:
            continue
        out.append(
            {
                "theme_id": theme.theme_id,
                "title": theme.title,
                "severity": theme.severity,
                "category": theme.category,
                "sectors": list(theme.sectors),
                "items": int(row["items"]),
            }
        )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda item: (order[str(item["severity"])], -int(item["items"]), str(item["theme_id"])))


def active_high_themes_by_day(
    conn: sqlite3.Connection, *, themes: list[Theme], min_items: int, window_days: int = 3
) -> dict[date, list[str]]:
    """Per day, the high-severity themes active then, for the market-state signal."""
    titles = {theme.theme_id: theme.title for theme in themes if theme.severity == "high"}
    daily: dict[str, dict[date, int]] = defaultdict(dict)
    for row in conn.execute("SELECT theme_id, day, item_count FROM theme_activity_daily"):
        if row["theme_id"] in titles:
            daily[row["theme_id"]][date.fromisoformat(row["day"])] = int(row["item_count"])
    first_row = conn.execute("SELECT MIN(COALESCE(published_at, first_seen_at)) AS first FROM watch_items").fetchone()
    if first_row is None or first_row["first"] is None:
        return {}
    day = item_day(first_row["first"], first_row["first"])
    last = _jakarta_today()
    out: dict[date, list[str]] = {}
    while day <= last:
        out[day] = sorted(
            titles[theme_id]
            for theme_id, days in daily.items()
            if sum(days.get(day - timedelta(days=k), 0) for k in range(window_days)) >= min_items
        )
        day += timedelta(days=1)
    return out


# --- findings -----------------------------------------------------------------


def _anomaly_findings(conn: sqlite3.Connection, cfg: Config, *, themes: list[Theme], today: date) -> list[Finding]:
    from .signals import load_market_series

    series = load_market_series(conn)
    if not series.get("index:COMPOSITE"):
        return []
    anomalies = detect_anomalies(
        series, sectors=cfg.market.sectors, since=today - timedelta(days=ANOMALY_LOOKBACK_DAYS), foreign_series=FOREIGN_NET_SERIES
    )
    catalyst_days = {
        date.fromisoformat(row["catalyst_date"])
        for row in conn.execute("SELECT catalyst_date FROM market_catalysts WHERE severity = 'high'")
    }
    by_day: dict[date, list[Anomaly]] = defaultdict(list)
    for anomaly in anomalies:
        if not any(abs((anomaly.trade_date - day).days) <= 1 for day in catalyst_days):
            by_day[anomaly.trade_date].append(anomaly)
    findings = []
    for day, found in sorted(by_day.items()):
        index = next((item for item in found if item.series_id == "index:COMPOSITE"), None)
        if index is None:
            findings.extend(_research_item(conn, anomaly, themes=themes) for anomaly in found)
            continue
        # A market-wide move is one research item; its sectors and flows go in its details.
        finding = _research_item(conn, index, themes=themes)
        others = [item for item in found if item is not index]
        if others:
            finding.details["also_unusual"] = [
                {"series_id": item.series_id, "label": item.label, "value": item.value, "zscore": item.zscore} for item in others
            ]
            finding.details["note"] = (
                f"{finding.details['note']} Also beyond its usual range that day: "
                + ", ".join(item.series_id.split(":", 1)[1] for item in others[:6])
                + (f" and {len(others) - 6} more" if len(others) > 6 else "")
                + "."
            )
        findings.append(finding)
    return findings


def _research_item(conn: sqlite3.Connection, anomaly: Anomaly, *, themes: list[Theme]) -> Finding:
    """An abnormal move with the watched items around it, grouped by theme (5e)."""
    start, end = anomaly.trade_date - timedelta(days=2), anomaly.trade_date + timedelta(days=1)
    rows = conn.execute(
        """
        SELECT wi.item_id, wi.title, wi.url, wi.published_at, wi.first_seen_at, wi.source_id,
               GROUP_CONCAT(DISTINCT it.theme_id) AS theme_ids
        FROM watch_items wi LEFT JOIN item_themes it ON it.item_id = wi.item_id
        WHERE COALESCE(wi.published_at, wi.first_seen_at) >= ? AND COALESCE(wi.published_at, wi.first_seen_at) < ?
        GROUP BY wi.item_id
        """,
        ((start - timedelta(days=1)).isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()
    window = [row for row in rows if start <= item_day(row["published_at"], row["first_seen_at"]) <= end]
    titles = {theme.theme_id: theme.title for theme in themes}
    by_theme: Counter[str] = Counter()
    for row in window:
        for theme_id in (row["theme_ids"] or "").split(","):
            if theme_id:
                by_theme[theme_id] += 1
    keywords = {keyword for theme in themes for keyword in theme.keywords}
    untagged_terms: Counter[str] = Counter()
    for row in window:
        if not row["theme_ids"]:
            for term in terms(row["title"]):
                if term not in keywords and " " in term:
                    untagged_terms[term] += 1
    direction = "rose" if anomaly.value > 0 else "fell"
    note = f"{anomaly.label} {direction} to {anomaly.value:+g} on {anomaly.trade_date} ({anomaly.zscore:+.1f} standard deviations)."
    if by_theme:
        note += " Watched items around it: " + ", ".join(f"{titles.get(k, k)} ({n})" for k, n in by_theme.most_common(4)) + "."
    elif window:
        note += f" {len(window)} watched items around it match no theme."
    else:
        note += " No watched items around it."
    candidates = [term for term, count in untagged_terms.most_common(5) if count >= 2]
    if candidates:
        note += " Candidate new themes: " + ", ".join(candidates) + "."
    headlines = [
        {"item_id": row["item_id"], "title": row["title"], "url": row["url"], "themes": (row["theme_ids"] or "").split(",") if row["theme_ids"] else []}
        for row in sorted(window, key=lambda item: (not item["theme_ids"], item["published_at"] or ""))[:8]
    ]
    return Finding(
        kind="anomaly",
        term=f"{anomaly.series_id} {anomaly.trade_date}",
        first_seen=anomaly.trade_date,
        detected_on=anomaly.trade_date,
        item_ids=[item["item_id"] for item in headlines],
        burst_ratio=abs(anomaly.zscore),
        details={
            "series_id": anomaly.series_id,
            "label": anomaly.label,
            "value": anomaly.value,
            "zscore": anomaly.zscore,
            "note": note,
            "themes": dict(by_theme.most_common()),
            "candidate_themes": candidates,
            "headlines": headlines,
        },
    )


def _store_findings(conn: sqlite3.Connection, *, findings: list[Finding], themes: list[Theme], today: date) -> tuple[int, int]:
    from ..intelligence.pipeline import DERIVED_VERSION

    now = datetime.now(timezone.utc).isoformat()
    new = updated = 0
    for finding in findings:
        existing = conn.execute(
            """
            SELECT finding_id, status, item_ids_json FROM emerging_findings
            WHERE kind = ? AND term_or_entity = ? AND detected_on >= ?
            ORDER BY detected_on DESC LIMIT 1
            """,
            (finding.kind, finding.term, (today - timedelta(days=FINDING_MERGE_DAYS)).isoformat()),
        ).fetchone()
        if existing is not None:
            if existing["status"] != "open":
                continue  # dismissed or promoted already; do not raise it again
            finding_id = existing["finding_id"]
            items = sorted(set(json.loads(existing["item_ids_json"])) | set(finding.item_ids))
            conn.execute(
                "UPDATE emerging_findings SET item_ids_json = ?, burst_ratio = ?, details_json = ?, updated_at = ? WHERE finding_id = ?",
                (json.dumps(items), finding.burst_ratio, json.dumps(finding.details, default=str), now, finding_id),
            )
            updated += 1
        else:
            finding_id = "finding-" + hashlib.sha1(f"{finding.kind}|{finding.term}|{today}".encode("utf-8")).hexdigest()[:20]
            items = finding.item_ids
            conn.execute(
                """
                INSERT INTO emerging_findings (
                    finding_id, kind, term_or_entity, first_seen, detected_on, burst_ratio, item_ids_json,
                    details_json, status, theme_id, review_item_id, detector_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?, ?)
                """,
                (
                    finding_id,
                    finding.kind,
                    finding.term,
                    finding.first_seen.isoformat(),
                    finding.detected_on.isoformat(),
                    finding.burst_ratio,
                    json.dumps(items),
                    json.dumps(finding.details, default=str),
                    DETECTOR_VERSION,
                    now,
                ),
            )
            new += 1
        review_item_id = _upsert_review_item(conn, finding_id=finding_id, finding=finding, item_count=len(items), derived_version=DERIVED_VERSION, now=now)
        conn.execute("UPDATE emerging_findings SET review_item_id = ? WHERE finding_id = ?", (review_item_id, finding_id))
    return new, updated


def _upsert_review_item(
    conn: sqlite3.Connection, *, finding_id: str, finding: Finding, item_count: int, derived_version: str, now: str
) -> str:
    item_type = ANOMALY_ITEM_TYPE if finding.kind == "anomaly" else FINDING_ITEM_TYPE
    review_item_id = "review-" + hashlib.sha1(f"{item_type}|{finding_id}|{derived_version}".encode("utf-8")).hexdigest()[:24]
    if finding.kind == "anomaly":
        rationale = str(finding.details.get("note"))
        priority = "high"
    elif finding.kind == "burst":
        rationale = (
            f"'{finding.term}' is in {finding.details.get('count')} headlines this week "
            f"({finding.details.get('share_pct')}% of them, against {finding.details.get('usual_share_pct')}% usually); "
            f"first seen {finding.first_seen}."
        )
        priority = "high" if (finding.burst_ratio or 0) >= 8 else "medium"
    elif finding.kind == "new_pairing":
        rationale = f"'{finding.term}' appeared next to Indonesia or IHSG for the first time ({item_count} headlines), e.g. \"{finding.details.get('example')}\"."
        priority = "medium"
    else:
        rationale = f"A high-trust item matched no theme: \"{finding.details.get('title')}\"."
        priority = "medium"
    payload = json.dumps({"finding_id": finding_id, "kind": finding.kind, "term": finding.term, **finding.details}, default=str)
    conn.execute(
        """
        INSERT INTO review_queue_items (
            review_item_id, item_type, ticker, document_id, source_key, priority, status, rationale,
            payload_json, derived_version, created_at, updated_at
        ) VALUES (?, ?, NULL, NULL, ?, ?, 'open', ?, ?, ?, ?, ?)
        ON CONFLICT(item_type, source_key, derived_version) DO UPDATE SET
            priority = excluded.priority, rationale = excluded.rationale,
            payload_json = excluded.payload_json, updated_at = excluded.updated_at
        """,
        (review_item_id, item_type, finding_id, priority, rationale, payload, derived_version, now, now),
    )
    return review_item_id


def _ensure_catalog_review(conn: sqlite3.Connection, *, themes: list[Theme], today: date) -> None:
    """Once a quarter, a review item to re-read the catalog against what the quarter raised."""
    from ..intelligence.pipeline import DERIVED_VERSION

    quarter = f"{today.year}Q{(today.month - 1) // 3 + 1}"
    start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    fired = {row["theme_id"] for row in conn.execute("SELECT DISTINCT theme_id FROM theme_activity_daily WHERE day >= ?", (start.isoformat(),))}
    findings = Counter(
        row["kind"] for row in conn.execute("SELECT kind FROM emerging_findings WHERE detected_on >= ?", (start.isoformat(),))
    )
    silent = sorted(theme.theme_id for theme in themes if theme.theme_id not in fired)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "quarter": quarter,
        "themes_fired": len(fired),
        "themes_silent": silent,
        "findings": dict(findings),
        "catalog": "docs/market_risk_factors.md",
    }
    rationale = (
        f"Quarterly catalog review for {quarter}: {len(fired)} themes fired, {len(silent)} stayed silent, "
        f"{sum(findings.values())} findings so far. Re-read docs/market_risk_factors.md against them."
    )
    review_item_id = "review-" + hashlib.sha1(f"{CATALOG_ITEM_TYPE}|{quarter}|{DERIVED_VERSION}".encode("utf-8")).hexdigest()[:24]
    conn.execute(
        """
        INSERT INTO review_queue_items (
            review_item_id, item_type, ticker, document_id, source_key, priority, status, rationale,
            payload_json, derived_version, created_at, updated_at
        ) VALUES (?, ?, NULL, NULL, ?, 'low', 'open', ?, ?, ?, ?, ?)
        ON CONFLICT(item_type, source_key, derived_version) DO UPDATE SET
            rationale = excluded.rationale, payload_json = excluded.payload_json, updated_at = excluded.updated_at
        """,
        (review_item_id, CATALOG_ITEM_TYPE, f"catalog:{quarter}", rationale, json.dumps(payload), DERIVED_VERSION, now, now),
    )


# --- review actions -----------------------------------------------------------


def promote_finding(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    theme_id: str,
    title: str,
    keywords: list[str],
    severity: str,
    sectors: list[str],
    category: str | None,
    note: str | None = None,
) -> None:
    """Make a finding a tracked theme; the next watch-sync tags recent items with it."""
    from ..intelligence.pipeline import record_review_transition

    row = conn.execute("SELECT status, review_item_id FROM emerging_findings WHERE finding_id = ?", (finding_id,)).fetchone()
    if row is None:
        raise ValueError(f"finding not found: {finding_id}")
    if severity not in SEVERITY_WEIGHT:
        raise ValueError(f"severity must be low, medium or high, not {severity!r}")
    cleaned = sorted({keyword.strip().lower() for keyword in keywords if keyword.strip()})
    if not cleaned:
        raise ValueError("a promoted theme needs at least one keyword")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO risk_themes (theme_id, category, title, severity, keywords_json, sectors_json, origin, finding_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'promoted', ?, ?)
        ON CONFLICT(theme_id) DO UPDATE SET
            category = excluded.category, title = excluded.title, severity = excluded.severity,
            keywords_json = excluded.keywords_json, sectors_json = excluded.sectors_json, finding_id = excluded.finding_id
        """,
        (theme_id, category, title, severity, json.dumps(cleaned), json.dumps(sorted({s.upper() for s in sectors})), finding_id, now),
    )
    conn.execute(
        "UPDATE emerging_findings SET status = 'promoted', theme_id = ?, updated_at = ? WHERE finding_id = ?",
        (theme_id, now, finding_id),
    )
    if row["review_item_id"]:
        _close_review_item(conn, row["review_item_id"], note=note or f"promoted to theme {theme_id}", record=record_review_transition)


def dismiss_finding(conn: sqlite3.Connection, *, finding_id: str, note: str | None = None) -> None:
    from ..intelligence.pipeline import record_review_transition

    row = conn.execute("SELECT review_item_id FROM emerging_findings WHERE finding_id = ?", (finding_id,)).fetchone()
    if row is None:
        raise ValueError(f"finding not found: {finding_id}")
    conn.execute(
        "UPDATE emerging_findings SET status = 'dismissed', updated_at = ? WHERE finding_id = ?",
        (datetime.now(timezone.utc).isoformat(), finding_id),
    )
    if row["review_item_id"]:
        _close_review_item(conn, row["review_item_id"], note=note or "dismissed", record=record_review_transition)


def _close_review_item(conn: sqlite3.Connection, review_item_id: str, *, note: str, record) -> None:
    current = conn.execute("SELECT status, payload_json FROM review_queue_items WHERE review_item_id = ?", (review_item_id,)).fetchone()
    if current is None or current["status"] == "resolved":
        return
    record(
        conn,
        review_item_id=review_item_id,
        ticker=None,
        action="finding_review",
        from_status=current["status"],
        to_status="resolved",
        note=note,
        payload_json=current["payload_json"],
    )


def load_open_findings(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT finding_id, kind, term_or_entity, first_seen, detected_on, burst_ratio, item_ids_json, details_json
        FROM emerging_findings WHERE status = 'open'
        ORDER BY CASE kind WHEN 'anomaly' THEN 0 WHEN 'burst' THEN 1 WHEN 'new_pairing' THEN 2 ELSE 3 END,
                 detected_on DESC, burst_ratio DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "finding_id": row["finding_id"],
            "kind": row["kind"],
            "term": row["term_or_entity"],
            "first_seen": row["first_seen"],
            "detected_on": row["detected_on"],
            "burst_ratio": row["burst_ratio"],
            "item_count": len(json.loads(row["item_ids_json"])),
            "details": json.loads(row["details_json"]),
        }
        for row in rows
    ]


def load_theme_headlines(conn: sqlite3.Connection, *, theme_id: str, limit: int = 5) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT wi.title, wi.url, wi.source_id, COALESCE(wi.published_at, wi.first_seen_at) AS stamp, it.evidence_text
        FROM item_themes it JOIN watch_items wi ON wi.item_id = it.item_id
        WHERE it.theme_id = ?
        ORDER BY stamp DESC LIMIT ?
        """,
        (theme_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# --- replay -------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayHit:
    kind: str
    term: str
    detected_on: date
    first_seen: date
    burst_ratio: float | None
    example: str | None


def replay_findings(conn: sqlite3.Connection, *, start: date, end: date, match: str | None = None) -> list[ReplayHit]:
    """What the detectors would have raised on each day, using only items seen by then.

    Read-only: nothing is stored, so replaying old news never fills the review queue.
    """
    headlines = load_headlines(conn, today=end, days=(end - start).days + HEADLINE_DAYS)
    first_hits: dict[tuple[str, str], ReplayHit] = {}
    by_id = {headline.item_id: headline for headline in headlines}
    day = start
    while day <= end:
        for finding in detect_bursts(headlines, as_of=day) + detect_new_pairings(headlines, as_of=day):
            if match and match.lower() not in finding.term:
                continue
            key = (finding.kind, finding.term)
            if key not in first_hits:
                example = next((by_id[item_id].title for item_id in finding.item_ids if item_id in by_id), None)
                first_hits[key] = ReplayHit(finding.kind, finding.term, day, finding.first_seen, finding.burst_ratio, example)
        day += timedelta(days=1)
    return sorted(first_hits.values(), key=lambda hit: (hit.detected_on, -(hit.burst_ratio or 0.0), hit.term))


def _jakarta_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()
