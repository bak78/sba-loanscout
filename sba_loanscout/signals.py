"""Rules engine: match signals against page text and capture evidence.

Two invariants keep scores honest and are covered by tests:

1. Distinct signal ids are counted, never occurrences. A job board repeating
   "now hiring" forty times contributes hiring_active's weight exactly once.
2. A match preceded by a negation inside the configured window is discarded,
   so "we are not going out of business" is not a distress signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Rules, Signal
from .extract import Page

SNIPPET_RADIUS = 120

_SNIPPET_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SignalHit:
    """One signal firing on one page, with the text that triggered it."""

    signal_id: str
    category: str
    weight: float
    snippet: str
    source_url: str
    page_type: str

    def describe(self) -> str:
        return f"[{self.signal_id}] “{self.snippet}” ({self.source_url})"


def _snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - SNIPPET_RADIUS)
    right = min(len(text), end + SNIPPET_RADIUS)
    # Collapsed to one line so evidence stays readable in a spreadsheet cell.
    fragment = _SNIPPET_WHITESPACE.sub(" ", text[left:right]).strip()
    return f"{'…' if left > 0 else ''}{fragment}{'…' if right < len(text) else ''}"


def _is_negated(text: str, start: int, rules: Rules) -> bool:
    """True if a negation appears within the window preceding a match."""
    window_start = max(0, start - rules.negation_window)
    return rules.negation_regex.search(text, window_start, start) is not None


def match_page(page: Page, rules: Rules) -> list[SignalHit]:
    """All signals firing on one page. At most one hit per signal per page."""
    text = page.searchable
    hits: list[SignalHit] = []

    for signal in rules.signals:
        hit = _first_match(signal, text, page, rules)
        if hit is not None:
            hits.append(hit)

    return hits


def _first_match(
    signal: Signal, text: str, page: Page, rules: Rules
) -> SignalHit | None:
    """First non-negated match for a signal, or None.

    Stops at the first surviving match: additional occurrences cannot change
    the score, so scanning past it would only cost time.
    """
    for regex in signal.regexes:
        for match in regex.finditer(text):
            if _is_negated(text, match.start(), rules):
                continue

            multiplier = signal.page_bonus.get(page.page_type, 1.0)
            return SignalHit(
                signal_id=signal.id,
                category=signal.category,
                weight=signal.weight * multiplier,
                snippet=_snippet(text, match.start(), match.end()),
                source_url=page.url,
                page_type=page.page_type,
            )
    return None


def match_site(pages: list[Page], rules: Rules) -> list[SignalHit]:
    """Signals across a whole site, deduped to the best hit per signal.

    When a signal fires on several pages, the highest-weight hit wins — that
    is the one carrying the page bonus (hiring found on /careers beats hiring
    mentioned in passing on the homepage).
    """
    best: dict[str, SignalHit] = {}

    for page in pages:
        for hit in match_page(page, rules):
            incumbent = best.get(hit.signal_id)
            if incumbent is None or hit.weight > incumbent.weight:
                best[hit.signal_id] = hit

    return sorted(best.values(), key=lambda h: h.weight, reverse=True)
