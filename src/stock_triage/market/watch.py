"""Watched sources (docs/market_risk_plan.md, component 5a).

``config/watch_sources.yaml`` lists them; adding one is a YAML edit. The fetch
kinds cover what plain HTTP can read (docs/phase4_sources_recon.md):

- ``rss``: an RSS 2.0 or Atom feed;
- ``html_links``: a page listing press releases as links (Bank Indonesia, OJK);
- ``sitemap``: an XML sitemap whose URLs carry the title (Fitch research);
- ``json_list``: a JSON list of items (WHO outbreak news, BMKG earthquakes);
- ``gdelt``: the GDELT DOC 2.0 article index, a wide net over world news that
  can also reach back months; it throttles hard, so a refused call is skipped;
- ``google_news``: a Google News search feed. Its robots.txt disallows
  automated access, so the shipped entry is disabled.

Every kind takes ``match`` (keep items containing any of these words) and
``exclude`` (drop items containing any). Only the headline, a short excerpt and
the link are kept, never full text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urljoin

import httpx
import yaml

FETCH_KINDS = ("rss", "html_links", "sitemap", "json_list", "gdelt", "google_news")
TRUST_LEVELS = ("high", "medium", "low")
EXCERPT_CHARS = 280
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
# GDELT asks for no more than one request every five seconds, and in practice
# refuses many more (docs/phase4_sources_recon.md).
GDELT_PAUSE_S = 5.5
GDELT_RETRY_PAUSE_S = 30.0
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class WatchSourceError(ValueError):
    """The watch registry could not be read."""


@dataclass(frozen=True)
class WatchSource:
    source_id: str
    name: str
    kind: str
    trust: str
    url: str | None = None
    query: str | None = None
    language: str | None = None
    enabled: bool = True
    notes: str | None = None
    match: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    options: dict[str, object] = field(default_factory=dict)  # kind-specific settings


@dataclass(frozen=True)
class WatchItem:
    source_id: str
    title: str
    url: str
    published_at: datetime | None
    excerpt: str | None
    language: str | None
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def item_id(self) -> str:
        key = str(self.raw.get("guid") or self.url)
        return hashlib.sha256(f"{self.source_id}|{key}".encode("utf-8")).hexdigest()[:24]


def load_watch_sources(path: Path) -> list[WatchSource]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WatchSourceError(f"{path}: not valid YAML: {exc}") from exc
    sources: list[WatchSource] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw.get("sources") or []):
        source_id = str(entry.get("source_id") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        trust = str(entry.get("trust") or "medium").strip()
        if not source_id:
            raise WatchSourceError(f"{path}: source {index} needs a source_id")
        if source_id in seen:
            raise WatchSourceError(f"{path}: source_id {source_id} appears twice")
        if kind not in FETCH_KINDS:
            raise WatchSourceError(f"{path}: {source_id} has kind {kind!r}; expected one of {', '.join(FETCH_KINDS)}")
        if trust not in TRUST_LEVELS:
            raise WatchSourceError(f"{path}: {source_id} has trust {trust!r}; expected high, medium or low")
        if kind in {"rss", "html_links", "sitemap", "json_list"} and not entry.get("url"):
            raise WatchSourceError(f"{path}: {source_id} is a {kind} source and needs a url")
        if kind == "html_links" and not entry.get("link_pattern"):
            raise WatchSourceError(f"{path}: {source_id} needs a link_pattern (a regular expression for the links to keep)")
        if kind == "json_list" and not (entry.get("title_field") or entry.get("title_template")):
            raise WatchSourceError(f"{path}: {source_id} needs a title_field or a title_template")
        if kind in {"gdelt", "google_news"} and not entry.get("query"):
            raise WatchSourceError(f"{path}: {source_id} is a {kind} source and needs a query")
        seen.add(source_id)
        sources.append(
            WatchSource(
                source_id=source_id,
                name=str(entry.get("name") or source_id),
                kind=kind,
                trust=trust,
                url=entry.get("url"),
                query=entry.get("query"),
                language=entry.get("language"),
                enabled=bool(entry.get("enabled", True)),
                notes=entry.get("notes"),
                match=tuple(str(word).lower() for word in entry.get("match") or []),
                exclude=tuple(str(word).lower() for word in entry.get("exclude") or []),
                options={key: value for key, value in entry.items() if key in _OPTION_KEYS},
            )
        )
    return sources


_OPTION_KEYS = frozenset(
    {"link_pattern", "items_path", "title_field", "title_template", "date_field", "url_field", "url_template",
     "summary_field", "min_values", "base_url"}
)


# --- parsing ------------------------------------------------------------------


def parse_feed(content: bytes, *, source: WatchSource) -> list[WatchItem]:
    """Items from an RSS 2.0 or Atom document. DTDs and entities are refused."""
    head = content[:4096].lower()
    if b"<!entity" in head:
        raise ValueError("feed declares XML entities; refused")
    text = re.sub(rb"<!DOCTYPE[^>\[]*>", b"", content, count=1, flags=re.IGNORECASE)
    root = ET.fromstring(text)
    items: list[WatchItem] = []
    if _local(root.tag) == "feed":  # Atom
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            title = _clean(_child_text(entry, "title"))
            link = next(
                (child.get("href") for child in entry if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate"),
                None,
            )
            published = _child_text(entry, "published") or _child_text(entry, "updated")
            summary = _child_text(entry, "summary") or _child_text(entry, "content")
            guid = _child_text(entry, "id")
            if title and link:
                items.append(
                    WatchItem(
                        source_id=source.source_id,
                        title=title,
                        url=link,
                        published_at=_parse_iso(published),
                        excerpt=_excerpt(summary, title),
                        language=source.language,
                        raw={"guid": guid, "published": published},
                    )
                )
        return items
    channel = next((child for child in root if _local(child.tag) == "channel"), root)
    for entry in channel:
        if _local(entry.tag) != "item":
            continue
        title = _clean(_child_text(entry, "title"))
        link = (_child_text(entry, "link") or "").strip()
        published = _child_text(entry, "pubDate") or _child_text(entry, "date")
        guid = _child_text(entry, "guid")
        outlet = _child_text(entry, "source")
        if source.kind == "google_news" and outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3].strip()
        if title and link:
            items.append(
                WatchItem(
                    source_id=source.source_id,
                    title=title,
                    url=link,
                    published_at=_parse_rfc822(published) or _parse_iso(published),
                    excerpt=_excerpt(_child_text(entry, "description"), title),
                    language=source.language,
                    raw={"guid": guid, "published": published, "outlet": outlet},
                )
            )
    return items


def parse_gdelt(payload: dict[str, object], *, source: WatchSource) -> list[WatchItem]:
    items = []
    for article in payload.get("articles") or []:  # type: ignore[union-attr]
        title = _clean(str(article.get("title") or ""))
        url = str(article.get("url") or "")
        if not title or not url:
            continue
        items.append(
            WatchItem(
                source_id=source.source_id,
                title=title,
                url=url,
                published_at=_parse_gdelt_date(str(article.get("seendate") or "")),
                excerpt=None,
                language=str(article.get("language") or "") or source.language,
                raw={
                    "domain": article.get("domain"),
                    "sourcecountry": article.get("sourcecountry"),
                    "seendate": article.get("seendate"),
                },
            )
        )
    return items


# --- fetching -----------------------------------------------------------------


def fetch_source(
    client: httpx.Client,
    source: WatchSource,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[WatchItem]:
    """The source's current items, after its match and exclude filters.

    A GDELT source can also fetch a past window.
    """
    if source.kind == "gdelt":
        items = _fetch_gdelt(client, source, start=start, end=end)
    elif source.kind == "google_news":
        language = source.language or "id"
        url = f"{GOOGLE_NEWS_URL}?q={quote_plus(str(source.query))}&hl={language}&gl=ID&ceid=ID:{language}"
        items = parse_feed(_get(client, url).content, source=source)
    else:
        response = _get(client, str(source.url))
        if source.kind == "rss":
            items = parse_feed(response.content, source=source)
        elif source.kind == "html_links":
            items = parse_html_links(response.text, source=source, page_url=str(response.url))
        elif source.kind == "sitemap":
            items = parse_sitemap(response.content, source=source)
        else:
            items = parse_json_list(response.json(), source=source)
    return [item for item in items if _passes(item, source)]


def parse_html_links(page: str, *, source: WatchSource, page_url: str) -> list[WatchItem]:
    """Links whose address matches ``link_pattern``, titled by their text."""
    pattern = re.compile(str(source.options["link_pattern"]))
    base = str(source.options.get("base_url") or page_url)
    items: dict[str, WatchItem] = {}
    for href, text in re.findall(r"<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", page, re.S | re.I):
        if not pattern.search(href):
            continue
        title = _clean(text)
        url = urljoin(base, html.unescape(href))
        if len(title) < 12 or (url in items and len(items[url].title) >= len(title)):
            continue  # an image link or a shorter duplicate of the same item
        items[url] = WatchItem(source_id=source.source_id, title=title, url=url, published_at=None, excerpt=None,
                               language=source.language, raw={"guid": url})
    return list(items.values())


def parse_sitemap(content: bytes, *, source: WatchSource) -> list[WatchItem]:
    """URLs from an XML sitemap, titled from their last path segment."""
    if b"<!entity" in content[:4096].lower():
        raise ValueError("sitemap declares XML entities; refused")
    root = ET.fromstring(content)
    items = []
    for entry in root:
        if _local(entry.tag) != "url":
            continue
        loc = (_child_text(entry, "loc") or "").strip()
        if not loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-\d{2}-\d{2}-\d{4}$", "", slug)
        title = slug.replace("-", " ").strip().capitalize()
        lastmod = _child_text(entry, "lastmod")
        items.append(
            WatchItem(source_id=source.source_id, title=title, url=loc, published_at=_parse_iso(lastmod),
                      excerpt=None, language=source.language, raw={"guid": loc, "lastmod": lastmod})
        )
    return items


def parse_json_list(payload: object, *, source: WatchSource) -> list[WatchItem]:
    """Items from a JSON list at ``items_path``, with a title from a field or a template."""
    rows = payload
    for key in str(source.options.get("items_path") or "").split("."):
        if key:
            rows = rows.get(key, []) if isinstance(rows, dict) else []
    items = []
    minimums = source.options.get("min_values") or {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if any(_as_float(row.get(name)) < float(floor) for name, floor in minimums.items()):  # type: ignore[union-attr]
            continue
        title = _fill(str(source.options["title_template"]), row) if source.options.get("title_template") else str(row.get(source.options.get("title_field"), ""))
        title = _clean(title)
        url_value = row.get(source.options.get("url_field")) if source.options.get("url_field") else None
        url = _fill(str(source.options["url_template"]), row) if source.options.get("url_template") else str(url_value or "")
        if url and source.options.get("base_url"):
            url = urljoin(str(source.options["base_url"]), url)
        stamp = row.get(source.options.get("date_field")) if source.options.get("date_field") else None
        if not title or not url:
            continue
        summary = row.get(source.options.get("summary_field")) if source.options.get("summary_field") else None
        items.append(
            WatchItem(
                source_id=source.source_id,
                title=title,
                url=url,
                published_at=_parse_iso(str(stamp)) if stamp else None,
                excerpt=_excerpt(str(summary), title) if summary else None,
                language=source.language,
                raw={"guid": f"{url}|{stamp}"},
            )
        )
    return items


def gdelt_url(query: str, *, start: datetime | None, end: datetime | None, timespan: str = "3d") -> str:
    params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": "250", "sort": "datedesc"}
    if start is not None and end is not None:
        params["startdatetime"] = start.strftime("%Y%m%d%H%M%S")
        params["enddatetime"] = end.strftime("%Y%m%d%H%M%S")
    else:
        params["timespan"] = timespan
    return f"{GDELT_URL}?{urlencode(params)}"


def _fetch_gdelt(
    client: httpx.Client, source: WatchSource, *, start: datetime | None, end: datetime | None
) -> list[WatchItem]:
    if start is None or end is None:
        return _gdelt_call(client, source, gdelt_url(str(source.query), start=None, end=None))
    # A past window is fetched a day at a time: one call returns at most 250 articles.
    # A refused day is retried once after a pause, then skipped.
    items: list[WatchItem] = []
    skipped = 0
    day = start
    while day < end:
        until = min(day + timedelta(days=1), end)
        url = gdelt_url(str(source.query), start=day, end=until)
        for attempt in range(2):
            try:
                items.extend(_gdelt_call(client, source, url))
                break
            except GdeltThrottled:
                if attempt == 0:
                    time.sleep(GDELT_RETRY_PAUSE_S)
                else:
                    skipped += 1
        day = until
        if day < end:
            time.sleep(GDELT_PAUSE_S)
    if skipped and not items:
        raise GdeltThrottled(f"GDELT refused all {skipped} days of the window; try again later")
    return items


class GdeltThrottled(RuntimeError):
    """GDELT refused the call (HTTP 429); it asks for one call every five seconds."""


def _gdelt_call(client: httpx.Client, source: WatchSource, url: str) -> list[WatchItem]:
    response = client.get(url)
    if response.status_code == 429:
        raise GdeltThrottled("GDELT refused the call (429); try again later")
    response.raise_for_status()
    body = response.text.strip()
    if not body.startswith("{"):
        # GDELT answers a malformed query or a too-fast caller with a plain-text message.
        raise ValueError(f"GDELT returned a message instead of JSON: {body[:160]}")
    return parse_gdelt(json.loads(body), source=source)


def persist_items(conn: sqlite3.Connection, *, items: list[WatchItem], trust: str) -> list[str]:
    """Store items not seen before; returns the new item ids."""
    now = datetime.now(timezone.utc).isoformat()
    new_ids = []
    for item in items:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO watch_items (
                item_id, source_id, trust, published_at, first_seen_at, title, url, excerpt, language, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.source_id,
                trust,
                item.published_at.astimezone(timezone.utc).isoformat() if item.published_at else None,
                now,
                item.title,
                item.url,
                item.excerpt,
                item.language,
                json.dumps(item.raw, sort_keys=True, default=str),
            ),
        )
        if cursor.rowcount:
            new_ids.append(item.item_id)
    return new_ids


def record_source_run(
    conn: sqlite3.Connection, *, source_id: str, status: str, item_count: int, new_count: int, error: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO watch_source_runs (source_id, fetched_at, status, item_count, new_count, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, datetime.now(timezone.utc).isoformat(), status, item_count, new_count, error),
    )


def item_day(published_at: str | None, first_seen_at: str) -> date:
    """The Jakarta calendar day an item belongs to."""
    stamp = datetime.fromisoformat(published_at or first_seen_at)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp.astimezone(timezone.utc) + timedelta(hours=7)).date()


# --- helpers ------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return "".join(child.itertext()) if len(child) else (child.text or "")
    return None


def _clean(text: str | None) -> str:
    cleaned = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()
    # GDELT titles carry a space before punctuation ("naik 10 , 61 %").
    return re.sub(r"\s+([,.;:%!?)])", r"\1", cleaned)


def _get(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url)
    response.raise_for_status()
    return response


def _passes(item: WatchItem, source: WatchSource) -> bool:
    text = f"{item.title} {item.excerpt or ''} {item.url}".lower()
    if source.match and not any(word in text for word in source.match):
        return False
    return not any(word in text for word in source.exclude)


def _fill(template: str, row: dict) -> str:
    # Plain {field} substitution only; no attribute or index access.
    return re.sub(r"\{(\w+)\}", lambda match: str(row.get(match.group(1), "")), template)


def _as_float(value: object) -> float:
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return float("-inf")


def _excerpt(text: str | None, title: str) -> str | None:
    cleaned = _clean(text)
    if not cleaned or cleaned == title:
        return None
    if len(cleaned) <= EXCERPT_CHARS:
        return cleaned
    cut = cleaned[:EXCERPT_CHARS].rsplit(" ", 1)[0]
    return f"{cut}…"


def _parse_rfc822(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=7)))


def _parse_gdelt_date(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
