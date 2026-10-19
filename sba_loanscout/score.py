"""Combine signal hits into a 0-100 score with visible sub-scores.

Each category's raw weight sum is passed through a saturation curve
(100 * raw / (raw + k)) rather than a hard cap, so a site with many weak
signals and one with a few strong ones separate cleanly instead of both
pinning at 100.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import CATEGORIES, Rules
from .signals import SignalHit


@dataclass(frozen=True)
class Score:
    """A scored site: the total, its parts, and the evidence behind it."""

    total: float
    categories: dict[str, float]
    disqualified: bool
    disqualify_reason: str
    hits: list[SignalHit] = field(default_factory=list)

    def top_hits(self, limit: int = 3) -> list[SignalHit]:
        """Highest-weight qualifying hits, for the evidence columns."""
        return [h for h in self.hits if h.category in CATEGORIES][:limit]

    @property
    def signal_ids(self) -> list[str]:
        return [h.signal_id for h in self.hits if h.category in CATEGORIES]


def _saturate(raw: float, k: float) -> float:
    """Map an unbounded raw score onto 0-100. raw == k yields 50."""
    if raw <= 0:
        return 0.0
    return 100.0 * raw / (raw + k)


def score_site(hits: list[SignalHit], rules: Rules) -> Score:
    """Score one site from its deduped signal hits."""
    ordered = sorted(hits, key=lambda h: h.weight, reverse=True)

    disqualifiers = [h for h in ordered if h.category == "disqualify"]

    raw: dict[str, float] = {c: 0.0 for c in CATEGORIES}
    for hit in ordered:
        if hit.category in raw:
            raw[hit.category] += hit.weight

    categories = {
        c: round(_saturate(raw[c], rules.saturation[c]), 1) for c in CATEGORIES
    }

    if disqualifiers:
        # Row is still emitted, with sub-scores intact, so the rule that
        # killed it stays visible and tunable.
        return Score(
            total=0.0,
            categories=categories,
            disqualified=True,
            disqualify_reason=", ".join(
                sorted({h.signal_id for h in disqualifiers})
            ),
            hits=ordered,
        )

    total = sum(categories[c] * rules.category_weights[c] for c in CATEGORIES)

    return Score(
        total=round(total, 1),
        categories=categories,
        disqualified=False,
        disqualify_reason="",
        hits=ordered,
    )
