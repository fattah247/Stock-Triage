"""Novelty detectors: what nobody is tracking yet (docs/market_risk_plan.md, 5d and 5e).

All of them are plain counting, so every finding can say why it fired:

- **term bursts:** a word or two-word phrase in this week's headlines far more
  often than in its own recent past ("standalone" next to "MSCI");
- **new pairings:** a name or acronym appearing next to Indonesia, IHSG or the
  rupiah for the first time;
- **unclassified items:** a high-trust item no theme matches;
- **market anomalies (5e):** IHSG, a sector or foreign flows moving far beyond
  their usual range with no known risk date to explain it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import pstdev

DETECTOR_VERSION = "novelty-v1"
BURST_WINDOW_DAYS = 7
BASELINE_WEEKS = 8
BURST_MIN_COUNT = 3
BURST_RATIO = 4.0
MAX_BURSTS = 10
PAIRING_MIN_COUNT = 2
# Bursts and "first time" need a past to compare with: the eight weeks before the
# window must hold headlines on at least this many days.
BASELINE_MIN_DAYS = 14
# About one a day over the eight weeks; the smoothed share keeps a small baseline cautious.
BASELINE_MIN_HEADLINES = 60
ANOMALY_SIGMA = 2.5
ANOMALY_HISTORY = 250
ANCHORS = frozenset({"indonesia", "ihsg", "rupiah", "bei", "idx", "jakarta", "jci"})
_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ][0-9A-Za-zÀ-ÿ&'.-]*")
_ACRONYM = re.compile(r"\b[A-Z][A-Z&]{1,6}\b")
# Indonesian and English words too common in market headlines to mean anything alone.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it its
    may more most new no not of on or our out over said says she so than that the their them then there these they this
    to up us was we were what when which who why will with would after before about amid against all also any back
    because between down during each first last like many much next now off only other per since some still such under
    until very week while year years today yesterday latest live update updates news report reports
    ada adalah agar akan akhir aku anda antara apa atas bagi bahkan bakal banyak baru begini begitu belum besar bisa
    buat bukan cara dalam dan dari dengan di dia hal hari harus hingga ini itu jadi jangan jelang juga jika kali kami
    kata ke kembali kini kita lagi lain lalu lebih mana masih mau melalui menjadi menurut mereka meski minggu mulai
    namun naik oleh pada para per pekan saat sama sampai saja sebagai sebelum secara sedang segera sejak sekarang
    seperti serta sesuai setelah soal sudah tak tahun tak tanpa tapi telah tengah terbaru terhadap tersebut tetap
    tidak turun untuk usai wib yang yakni ya ternyata simak berikut penyebabnya hari-ini sebut bilang ungkap
    rp us$ usd idr persen percent pct
    """.split()
)
# Everyday market vocabulary: never a finding on its own or as a name, but kept
# inside two-word phrases ("standalone market", "status pasar").
GENERIC = frozenset(
    """
    saham bursa pasar modal emiten investor harga ekonomi bos buka stocks stock market markets shares
    """.split()
)


@dataclass(frozen=True)
class Headline:
    item_id: str
    day: date
    title: str
    trust: str
    tagged: bool


@dataclass(frozen=True)
class Finding:
    kind: str  # burst | new_pairing | unclassified | anomaly
    term: str
    first_seen: date
    detected_on: date
    item_ids: list[str]
    burst_ratio: float | None = None
    details: dict[str, object] = field(default_factory=dict)


def terms(title: str) -> set[str]:
    """Lower-case words and adjacent two-word phrases, without stopwords or numbers."""
    words = [word.strip(".-'").lower() for word in _TOKEN.findall(title)]
    words = [word for word in words if word]
    kept = [word if _meaningful(word) else None for word in words]
    out = {word for word in kept if word and word not in GENERIC}
    for left, right in zip(kept, kept[1:]):
        if left and right and not (left in GENERIC and right in GENERIC):
            out.add(f"{left} {right}")
    return out


def baseline_days(headlines: list[Headline], *, as_of: date) -> int:
    """Days with headlines in the eight weeks before this week's window."""
    window_start = as_of - timedelta(days=BURST_WINDOW_DAYS - 1)
    baseline_start = window_start - timedelta(weeks=BASELINE_WEEKS)
    return len({headline.day for headline in headlines if baseline_start <= headline.day < window_start})


def baseline_ready(headlines: list[Headline], *, as_of: date) -> bool:
    """Enough past headlines for "more than usual" and "first time" to mean anything."""
    window_start = as_of - timedelta(days=BURST_WINDOW_DAYS - 1)
    baseline_start = window_start - timedelta(weeks=BASELINE_WEEKS)
    past = [headline for headline in headlines if baseline_start <= headline.day < window_start]
    return len({headline.day for headline in past}) >= BASELINE_MIN_DAYS and len(past) >= BASELINE_MIN_HEADLINES


def detect_bursts(headlines: list[Headline], *, as_of: date) -> list[Finding]:
    """Terms far more common in this week's headlines than in the eight weeks before.

    The comparison is by share of headlines, not by count, so a week with more
    sources than the baseline does not make every word look new. The baseline
    share is smoothed, so a term never seen before needs real weight to burst.
    """
    window_start = as_of - timedelta(days=BURST_WINDOW_DAYS - 1)
    baseline_start = window_start - timedelta(weeks=BASELINE_WEEKS)
    in_window = [h for h in headlines if window_start <= h.day <= as_of]
    in_baseline = [h for h in headlines if baseline_start <= h.day < window_start]
    if len({h.day for h in in_baseline}) < BASELINE_MIN_DAYS or len(in_baseline) < BASELINE_MIN_HEADLINES or not in_window:
        return []
    window: dict[str, set[str]] = defaultdict(set)
    baseline: Counter[str] = Counter()
    first_seen: dict[str, date] = {}
    for headline in headlines:
        if headline.day > as_of:
            continue
        for term in terms(headline.title):
            first_seen[term] = min(first_seen.get(term, headline.day), headline.day)
    for headline in in_window:
        for term in terms(headline.title):
            window[term].add(headline.item_id)
    for headline in in_baseline:
        for term in terms(headline.title):
            baseline[term] += 1
    findings: list[Finding] = []
    for term, ids in window.items():
        count = len(ids)
        share = count / len(in_window)
        usual = (baseline[term] + 0.5) / len(in_baseline)
        ratio = share / usual
        if count >= BURST_MIN_COUNT and ratio >= BURST_RATIO:
            findings.append(
                Finding(
                    kind="burst",
                    term=term,
                    first_seen=first_seen[term],
                    detected_on=as_of,
                    item_ids=sorted(ids),
                    burst_ratio=round(ratio, 2),
                    details={
                        "count": count,
                        "share_pct": round(share * 100, 2),
                        "usual_share_pct": round(baseline[term] / len(in_baseline) * 100, 2),
                        "baseline_per_week": round(baseline[term] / BASELINE_WEEKS, 2),
                    },
                )
            )
    # On a big news day everything about the story bursts; the strongest carry it.
    return _drop_covered_words(findings)[:MAX_BURSTS]


def detect_new_pairings(headlines: list[Headline], *, as_of: date) -> list[Finding]:
    """Names and acronyms next to Indonesia, IHSG or the rupiah this week and never before."""
    if not baseline_ready(headlines, as_of=as_of):
        return []
    window_start = as_of - timedelta(days=BURST_WINDOW_DAYS - 1)
    known_names = _mid_sentence_names(headlines)
    before: set[str] = set()
    current: dict[str, list[Headline]] = defaultdict(list)
    for headline in sorted(headlines, key=lambda item: item.day):
        if headline.day > as_of:
            continue
        words = {word.lower() for word in _TOKEN.findall(headline.title)}
        if not words & ANCHORS:
            continue
        for entity in _entities(headline.title, known_names):
            if entity.lower() in ANCHORS:
                continue
            if headline.day < window_start:
                before.add(entity.lower())
            else:
                current[entity.lower()].append(headline)
    findings = []
    for entity, items in current.items():
        if entity in before or len({item.item_id for item in items}) < PAIRING_MIN_COUNT:
            continue
        findings.append(
            Finding(
                kind="new_pairing",
                term=entity,
                first_seen=min(item.day for item in items),
                detected_on=as_of,
                item_ids=sorted({item.item_id for item in items}),
                details={"count": len({item.item_id for item in items}), "example": items[0].title},
            )
        )
    return findings


def detect_unclassified(headlines: list[Headline], *, as_of: date, limit: int = 20) -> list[Finding]:
    """High-trust items from the last day that no theme matched."""
    recent = [
        headline
        for headline in headlines
        if headline.trust == "high" and not headline.tagged and as_of - timedelta(days=1) <= headline.day <= as_of
    ]
    return [
        Finding(
            kind="unclassified",
            term=headline.title[:120],
            first_seen=headline.day,
            detected_on=as_of,
            item_ids=[headline.item_id],
            details={"title": headline.title},
        )
        for headline in recent[:limit]
    ]


@dataclass(frozen=True)
class Anomaly:
    trade_date: date
    series_id: str
    label: str
    value: float
    zscore: float


def detect_anomalies(
    series: dict[str, dict[date, float]], *, sectors: list[str], since: date, foreign_series: str
) -> list[Anomaly]:
    """Days from ``since`` on when IHSG, a sector or foreign flows moved beyond their usual range."""
    found: list[Anomaly] = []
    composite = series.get("index:COMPOSITE", {})
    dates = sorted(composite)
    returns = _returns(composite, dates)
    found += _extremes(returns, dates, since=since, series_id="index:COMPOSITE", label="IHSG daily change (%)")
    for sector in sectors:
        values = series.get(f"index:{sector}", {})
        sector_dates = [day for day in dates if day in values]
        if len(sector_dates) < 60:
            continue
        sector_returns = _returns(values, sector_dates)
        found += _extremes(sector_returns, sector_dates, since=since, series_id=f"index:{sector}", label=f"{sector} daily change (%)")
        # Breaking from IHSG: the sector's move net of what IHSG's move implies.
        found += _decoupling(sector, sector_returns, returns, sector_dates, since=since)
    flows = series.get(foreign_series, {})
    flow_dates = sorted(flows)
    found += _extremes(
        {day: flows[day] / 1e12 for day in flow_dates},
        flow_dates,
        since=since,
        series_id=foreign_series,
        label="foreign net flow (Rp tn, estimated)",
    )
    return found


def _returns(values: dict[date, float], dates: list[date]) -> dict[date, float]:
    return {
        day: (values[day] / values[prev] - 1.0) * 100.0
        for prev, day in zip(dates, dates[1:])
        if values.get(prev)
    }


def _extremes(values: dict[date, float], dates: list[date], *, since: date, series_id: str, label: str) -> list[Anomaly]:
    ordered = [day for day in dates if day in values]
    found = []
    for i, day in enumerate(ordered):
        if day < since:
            continue
        history = [values[d] for d in ordered[max(0, i - ANOMALY_HISTORY) : i]]
        if len(history) < 60:
            continue
        mean = sum(history) / len(history)
        spread = pstdev(history)
        if spread <= 0:
            continue
        z = (values[day] - mean) / spread
        if abs(z) >= ANOMALY_SIGMA:
            found.append(Anomaly(day, series_id, label, round(values[day], 3), round(z, 2)))
    return found


def _decoupling(
    sector: str, sector_returns: dict[date, float], composite_returns: dict[date, float], dates: list[date], *, since: date
) -> list[Anomaly]:
    shared = [day for day in dates if day in sector_returns and day in composite_returns]
    found = []
    for i, day in enumerate(shared):
        if day < since:
            continue
        window = shared[max(0, i - ANOMALY_HISTORY) : i]
        if len(window) < 60:
            continue
        xs = [composite_returns[d] for d in window]
        ys = [sector_returns[d] for d in window]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        var_x = sum((x - mean_x) ** 2 for x in xs)
        if var_x <= 0:
            continue
        beta = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
        residuals = [y - (mean_y + beta * (x - mean_x)) for x, y in zip(xs, ys)]
        spread = pstdev(residuals)
        if spread <= 0:
            continue
        residual = sector_returns[day] - (mean_y + beta * (composite_returns[day] - mean_x))
        z = residual / spread
        if abs(z) >= ANOMALY_SIGMA + 0.5:  # a stricter bar: residuals are noisier
            found.append(Anomaly(day, f"index:{sector}", f"{sector} move apart from IHSG (pts)", round(residual, 3), round(z, 2)))
    return found


def _meaningful(word: str) -> bool:
    if len(word) < 3 or word in STOPWORDS:
        return False
    return not any(char.isdigit() for char in word)


def _entities(title: str, known_names: set[str]) -> set[str]:
    """Acronyms always; capitalised words only when the headline is not in title case.

    A headline's first word is capitalised anyway, so it counts only when the same
    word appears capitalised mid-sentence somewhere else ("Danantara takes ...").
    """
    found = set(_ACRONYM.findall(title))
    words = _TOKEN.findall(title)
    capitalised = [word for word in words if word[:1].isupper()]
    if words and len(capitalised) / len(words) < 0.6:
        for position, word in enumerate(words):
            clean = word.strip(".-'")
            if not word[:1].isupper() or len(clean) <= 3 or clean.lower() in STOPWORDS or clean.lower() in GENERIC:
                continue
            if position > 0 or clean.lower() in known_names:
                found.add(clean)
    return {entity for entity in found if not any(char.isdigit() for char in entity)}


def _mid_sentence_names(headlines: list[Headline]) -> set[str]:
    names: set[str] = set()
    for headline in headlines:
        words = _TOKEN.findall(headline.title)
        capitalised = [word for word in words if word[:1].isupper()]
        if words and len(capitalised) / len(words) < 0.6:
            names.update(word.strip(".-'").lower() for word in words[1:] if word[:1].isupper())
    return names


def _drop_covered_words(findings: list[Finding]) -> list[Finding]:
    """A single word bursting only inside a bursting phrase is the same finding."""
    phrases = [finding for finding in findings if " " in finding.term]
    kept = []
    for finding in findings:
        if " " not in finding.term and any(
            finding.term in phrase.term.split() and set(finding.item_ids) <= set(phrase.item_ids) for phrase in phrases
        ):
            continue
        kept.append(finding)
    return sorted(kept, key=lambda finding: (-(finding.burst_ratio or 0.0), finding.term))

