"""Risk themes and tagging (docs/market_risk_plan.md, component 5c).

Keyword rules tag first. The local LLM tags what they miss, and its tag is kept
only when the words it quotes appear verbatim in the item: that proves the
evidence exists, not that the tag is right, which is why promotions stay with
a human.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

TAGGER_VERSION = "themes-v1"
SEVERITIES = ("low", "medium", "high")
SEVERITY_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 3.0}


class ThemeFileError(ValueError):
    """The themes file could not be read."""


@dataclass(frozen=True)
class Theme:
    theme_id: str
    title: str
    severity: str
    keywords: tuple[str, ...]
    category: str | None = None
    require_any: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    origin: str = "file"  # file | promoted

    def patterns(self) -> list[re.Pattern[str]]:
        return [_word_pattern(keyword) for keyword in self.keywords]


@dataclass(frozen=True)
class Tag:
    theme_id: str
    method: str  # keyword | llm
    field: str  # title | excerpt
    start: int
    text: str


def load_theme_file(path: Path) -> list[Theme]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ThemeFileError(f"{path}: not valid YAML: {exc}") from exc
    themes: list[Theme] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw.get("themes") or []):
        theme_id = str(entry.get("theme_id") or "").strip()
        keywords = tuple(str(word).strip().lower() for word in entry.get("keywords") or [] if str(word).strip())
        severity = str(entry.get("severity") or "medium").strip()
        if not theme_id or not keywords:
            raise ThemeFileError(f"{path}: theme {index} needs a theme_id and at least one keyword")
        if severity not in SEVERITIES:
            raise ThemeFileError(f"{path}: theme {theme_id} has severity {severity!r}; expected low, medium or high")
        if theme_id in seen:
            raise ThemeFileError(f"{path}: theme_id {theme_id} appears twice")
        seen.add(theme_id)
        themes.append(
            Theme(
                theme_id=theme_id,
                title=str(entry.get("title") or theme_id),
                severity=severity,
                keywords=keywords,
                category=entry.get("category"),
                require_any=tuple(str(word).strip().lower() for word in entry.get("require_any") or []),
                sectors=tuple(str(code).strip().upper() for code in entry.get("sectors") or []),
            )
        )
    return themes


def load_promoted_themes(conn: sqlite3.Connection) -> list[Theme]:
    return [
        Theme(
            theme_id=row["theme_id"],
            title=row["title"],
            severity=row["severity"],
            keywords=tuple(json.loads(row["keywords_json"])),
            category=row["category"],
            sectors=tuple(json.loads(row["sectors_json"])),
            origin="promoted",
        )
        for row in conn.execute(
            "SELECT theme_id, category, title, severity, keywords_json, sectors_json FROM risk_themes ORDER BY theme_id"
        )
    ]


def load_themes(conn: sqlite3.Connection, path: Path) -> list[Theme]:
    """The themes file plus promoted themes; a promoted theme replaces a file theme with its id."""
    promoted = load_promoted_themes(conn)
    promoted_ids = {theme.theme_id for theme in promoted}
    return [theme for theme in load_theme_file(path) if theme.theme_id not in promoted_ids] + promoted


def keyword_tags(title: str, excerpt: str | None, themes: list[Theme]) -> list[Tag]:
    """One tag per matching theme, citing the first place a keyword appears."""
    fields = (("title", title or ""), ("excerpt", excerpt or ""))
    combined = f"{title or ''}\n{excerpt or ''}"
    tags: list[Tag] = []
    for theme in themes:
        if theme.require_any and not any(_word_pattern(word).search(combined) for word in theme.require_any):
            continue
        found: Tag | None = None
        for field, text in fields:
            for pattern in theme.patterns():
                match = pattern.search(text)
                if match and (found is None or (field, match.start()) < (found.field, found.start)):
                    found = Tag(theme.theme_id, "keyword", field, match.start(), match.group(0))
            if found is not None:
                break
        if found is not None:
            tags.append(found)
    return tags


def validate_llm_tag(answer: object, *, title: str, excerpt: str | None, themes: list[Theme]) -> Tag | None:
    """An LLM tag survives only with a known theme and evidence quoted verbatim from the item."""
    if not isinstance(answer, dict):
        return None
    theme_id, evidence = answer.get("theme_id"), answer.get("evidence")
    if not theme_id or not isinstance(evidence, str) or len(evidence.strip()) < 3:
        return None
    if theme_id not in {theme.theme_id for theme in themes}:
        return None
    evidence = evidence.strip()
    for field, text in (("title", title or ""), ("excerpt", excerpt or "")):
        start = text.find(evidence)
        if start >= 0:
            return Tag(str(theme_id), "llm", field, start, evidence)
    return None


@dataclass(frozen=True)
class LLMTagger:
    """Asks the configured local model for the one best theme, or none."""

    base_url: str
    model_name: str
    timeout_s: float = 60.0
    temperature: float = 0.0

    def tag(self, *, title: str, excerpt: str | None, themes: list[Theme], client: httpx.Client | None = None) -> Tag | None:
        return self.ask(title=title, excerpt=excerpt, themes=themes, client=client)[1]

    def ask(
        self, *, title: str, excerpt: str | None, themes: list[Theme], client: httpx.Client | None = None
    ) -> tuple[dict | None, Tag | None]:
        """The model's raw answer and the tag that survives validation (``llm-eval`` reads both)."""
        catalog = "\n".join(f"- {theme.theme_id}: {theme.title}" for theme in themes)
        schema = {
            "type": "object",
            "properties": {"theme_id": {"type": ["string", "null"]}, "evidence": {"type": ["string", "null"]}},
            "required": ["theme_id", "evidence"],
        }
        payload = {
            "model": self.model_name,
            "stream": False,
            # A one-word classification needs no reasoning trace; reasoning models
            # otherwise spend most of the time budget thinking.
            "think": False,
            "format": schema,
            "options": {"temperature": self.temperature},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You classify Indonesian market news into risk themes. Return JSON with theme_id (one id from "
                        "the list, or null when none fits) and evidence (words copied exactly from the headline or "
                        "excerpt that show the theme, or null). Never paraphrase the evidence. Most headlines are "
                        "routine: daily price moves, company news, events and lifestyle stories get null. Give a "
                        "theme only when the headline itself is about that risk; when unsure, answer null."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Themes:\n{catalog}\n\nHeadline: {title}\nExcerpt: {excerpt or ''}",
                },
            ],
        }
        post = client.post if client is not None else httpx.post
        response = post(self.base_url.rstrip("/") + "/chat", json=payload, timeout=self.timeout_s)
        response.raise_for_status()
        content = response.json()["message"]["content"]
        try:
            answer = json.loads(content)
        except json.JSONDecodeError:
            return None, None
        return answer, validate_llm_tag(answer, title=title, excerpt=excerpt, themes=themes)


def persist_tags(conn: sqlite3.Connection, *, item_id: str, tags: list[Tag]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO item_themes (
            item_id, theme_id, method, evidence_field, evidence_start, evidence_text, tagger_version, tagged_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, theme_id, method) DO UPDATE SET
            evidence_field = excluded.evidence_field, evidence_start = excluded.evidence_start,
            evidence_text = excluded.evidence_text, tagger_version = excluded.tagger_version,
            tagged_at = excluded.tagged_at
        """,
        [(item_id, tag.theme_id, tag.method, tag.field, tag.start, tag.text, TAGGER_VERSION, now) for tag in tags],
    )


@lru_cache(maxsize=4096)
def _word_pattern(word: str) -> re.Pattern[str]:
    # Whole words or phrases, case-insensitive; inner spaces match any run of whitespace.
    body = r"\s+".join(re.escape(part) for part in word.split())
    return re.compile(rf"(?<![\w]){body}(?![\w])", re.IGNORECASE)

