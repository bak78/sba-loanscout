"""Configuration loading: .env for secrets, signals.yaml for website scoring
rules. Which SBA businesses count as leads lives in sba.yaml, loaded by sba.py."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SIGNALS_PATH = PROJECT_ROOT / "config" / "signals.yaml"
DEFAULT_SBA_PATH = PROJECT_ROOT / "config" / "sba.yaml"
DATA_DIR = PROJECT_ROOT / "data"

CATEGORIES = ("growth", "distress", "fit")


class ConfigError(RuntimeError):
    """Raised with an actionable message when configuration is unusable."""


@dataclass(frozen=True)
class Signal:
    """One scoring rule, with its patterns pre-compiled."""

    id: str
    category: str
    weight: float
    regexes: tuple[re.Pattern[str], ...]
    page_bonus: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Rules:
    """Everything loaded from signals.yaml."""

    signals: tuple[Signal, ...]
    category_weights: dict[str, float]
    saturation: dict[str, float]
    negation_window: int
    negation_regex: re.Pattern[str]
    thin_content_chars: int
    page_types: dict[str, tuple[str, ...]]
    crawl_priority: tuple[str, ...]
    domain_blocklist: frozenset[str]
    tld_blocklist: frozenset[str]

    def by_category(self, category: str) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.category == category)

    def page_type(self, url: str) -> str:
        """Classify a URL into a page type; 'other' when nothing matches."""
        lowered = url.lower()
        for name, fragments in self.page_types.items():
            if any(fragment in lowered for fragment in fragments):
                return name
        return "other"


@dataclass(frozen=True)
class Settings:
    """Runtime settings: secrets from the environment, rules from YAML."""

    rules: Rules
    serper_api_key: str | None
    contact_email: str

    @property
    def user_agent(self) -> str:
        return f"SBALoanScoutBot/0.1 (+{self.contact_email})"

    def require_serper_key(self) -> str:
        if not self.serper_api_key:
            raise ConfigError(
                "SERPER_API_KEY is not set, so businesses can't be looked up.\n"
                "  Fix: copy .env.example to .env and add a key from "
                "https://serper.dev\n"
                "  Or:  preview the SBA list for free with --dry-run"
            )
        return self.serper_api_key


def _compile_signals(raw: list[dict]) -> tuple[Signal, ...]:
    signals: list[Signal] = []
    seen: set[str] = set()
    for entry in raw:
        sid = entry["id"]
        if sid in seen:
            raise ConfigError(f"duplicate signal id in signals.yaml: {sid!r}")
        seen.add(sid)

        category = entry["category"]
        if category not in (*CATEGORIES, "disqualify"):
            raise ConfigError(
                f"signal {sid!r} has unknown category {category!r}; "
                f"expected one of {(*CATEGORIES, 'disqualify')}"
            )

        regexes = []
        for pattern in entry["patterns"]:
            try:
                regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                raise ConfigError(
                    f"signal {sid!r} has an invalid regex {pattern!r}: {exc}"
                ) from exc

        signals.append(
            Signal(
                id=sid,
                category=category,
                weight=float(entry["weight"]),
                regexes=tuple(regexes),
                page_bonus=dict(entry.get("page_bonus") or {}),
            )
        )
    return tuple(signals)


def load_rules(path: Path | str = DEFAULT_SIGNALS_PATH) -> Rules:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"signals file not found: {path}")

    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    scoring = raw.get("scoring") or {}

    # Normalize category weights so they sum to 1.0 regardless of what's written.
    weights = {c: float(scoring.get("weights", {}).get(c, 0.0)) for c in CATEGORIES}
    total = sum(weights.values())
    if total <= 0:
        raise ConfigError("scoring.weights must include at least one positive weight")
    weights = {c: w / total for c, w in weights.items()}

    saturation = {
        c: float(scoring.get("saturation", {}).get(c, 25.0)) for c in CATEGORIES
    }
    for category, value in saturation.items():
        if value <= 0:
            raise ConfigError(
                f"scoring.saturation.{category} must be positive, got {value}"
            )

    negation_patterns = scoring.get("negation_patterns") or []
    # Joined into one alternation so the negation check is a single scan.
    negation_regex = re.compile(
        "|".join(re.escape(p) for p in negation_patterns) or r"(?!x)x",
        re.IGNORECASE,
    )

    return Rules(
        signals=_compile_signals(raw.get("signals") or []),
        category_weights=weights,
        saturation=saturation,
        negation_window=int(scoring.get("negation_window", 40)),
        negation_regex=negation_regex,
        thin_content_chars=int(scoring.get("thin_content_chars", 200)),
        page_types={
            k: tuple(v) for k, v in (raw.get("page_types") or {}).items()
        },
        crawl_priority=tuple(raw.get("crawl_priority") or []),
        domain_blocklist=frozenset(raw.get("domain_blocklist") or []),
        tld_blocklist=frozenset(raw.get("tld_blocklist") or []),
    )


def load_settings(signals_path: Path | str = DEFAULT_SIGNALS_PATH) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        rules=load_rules(signals_path),
        serper_api_key=os.environ.get("SERPER_API_KEY") or None,
        contact_email=os.environ.get("CRAWLER_CONTACT_EMAIL", "unknown@example.com"),
    )
