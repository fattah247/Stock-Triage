"""``llm-eval``: compare local models on the theme-tagging job (component 5c).

Each model gets the labelled headlines in ``config/theme_eval.yaml`` through
the same request ``watch-sync`` sends, after one untimed warm-up call so that
loading the model does not count against it. A model is scored on:

- **accuracy:** answers that match the label or a listed alternative;
- **recall:** real risks it recognised;
- **precision:** of the themes it gave, how many were right;
- **evidence failures:** themes dropped because the quoted words were not in the headline;
- **latency:** seconds per headline on this machine.

The recommended model has the best F1 (recall and precision together) among
those fast enough for ``watch-sync``'s time budget, with accuracy breaking ties.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from .discovery import LLM_ITEMS_PER_RUN, LLM_TIME_BUDGET_S
from .themes import LLMTagger, Theme

# Fast enough to tag a run's worth of new items within the budget, roughly.
MAX_MEDIAN_SECONDS = LLM_TIME_BUDGET_S / (LLM_ITEMS_PER_RUN / 2)


@dataclass(frozen=True)
class EvalCase:
    title: str
    expect: str | None
    also: frozenset[str | None] = frozenset()

    @property
    def accepted(self) -> frozenset[str | None]:
        return frozenset({self.expect}) | self.also


@dataclass
class ModelScore:
    model: str
    cases: int = 0
    correct: int = 0
    risks: int = 0
    risks_found: int = 0
    tagged: int = 0
    tagged_correct: int = 0
    evidence_failures: int = 0
    errors: int = 0
    seconds: list[float] = field(default_factory=list)
    misses: list[tuple[str, str | None, str | None]] = field(default_factory=list)  # title, expected, got

    @property
    def accuracy(self) -> float:
        return self.correct / self.cases if self.cases else 0.0

    @property
    def recall(self) -> float:
        return self.risks_found / self.risks if self.risks else 0.0

    @property
    def precision(self) -> float:
        return self.tagged_correct / self.tagged if self.tagged else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def median_seconds(self) -> float:
        return statistics.median(self.seconds) if self.seconds else float("inf")

    @property
    def p90_seconds(self) -> float:
        if not self.seconds:
            return float("inf")
        ordered = sorted(self.seconds)
        return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]

    @property
    def fast_enough(self) -> bool:
        return self.median_seconds <= MAX_MEDIAN_SECONDS


def load_eval_cases(path: Path) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = []
    for entry in raw.get("cases") or []:
        cases.append(
            EvalCase(
                title=str(entry["title"]),
                expect=entry.get("expect"),
                also=frozenset(entry.get("also") or []),
            )
        )
    return cases


def evaluate_model(
    tagger: LLMTagger, cases: list[EvalCase], themes: list[Theme], *, client: httpx.Client | None = None
) -> ModelScore:
    score = ModelScore(model=tagger.model_name)
    known = {theme.theme_id for theme in themes}
    try:
        tagger.ask(title="Warm-up: IHSG ditutup menguat", excerpt=None, themes=themes, client=client)
    except (httpx.HTTPError, KeyError, ValueError):
        pass  # the timed calls below record any real failure
    for case in cases:
        started = time.monotonic()
        try:
            answer, tag = tagger.ask(title=case.title, excerpt=None, themes=themes, client=client)
        except (httpx.HTTPError, KeyError, ValueError):
            score.errors += 1
            answer, tag = None, None
        score.seconds.append(time.monotonic() - started)
        got = tag.theme_id if tag is not None else None
        if answer and answer.get("theme_id") in known and tag is None:
            score.evidence_failures += 1
        score.cases += 1
        right = got in case.accepted
        score.correct += right
        if case.expect is not None:
            score.risks += 1
            score.risks_found += right and got is not None
        if got is not None:
            score.tagged += 1
            score.tagged_correct += right
        if not right:
            score.misses.append((case.title, case.expect, got))
    return score


def recommend(scores: list[ModelScore]) -> ModelScore | None:
    usable = [score for score in scores if score.fast_enough and not score.errors] or [s for s in scores if not s.errors]
    if not usable:
        return None
    return max(usable, key=lambda score: (round(score.f1, 3), score.accuracy, -score.median_seconds))


def installed_chat_models(base_url: str, client: httpx.Client | None = None) -> list[str]:
    """Local models that can complete text (embedding and image models are left out)."""
    http = client or httpx.Client(timeout=30)
    tags = http.get(base_url.rstrip("/") + "/tags").json().get("models", [])
    models = []
    for model in tags:
        name = model["name"]
        show = http.post(base_url.rstrip("/") + "/show", json={"model": name}).json()
        if "completion" in (show.get("capabilities") or ["completion"]) and "embedding" not in (show.get("capabilities") or []):
            models.append(name)
    return models
