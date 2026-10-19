"""Per-site crawling with URL-pattern prioritization.

An 8-page budget spent breadth-first lands on privacy policies. Spent on the
careers and news pages, it lands where growth and distress signals actually
live — so links are ranked by page type before anything is fetched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import tldextract

from .config import Rules
from .contacts import SiteContacts, summarize
from .extract import Page, build_page, extract_links
from .fetch import PoliteFetcher

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 8
# Past this many seconds on one site, stop and score the pages already fetched.
# Checked between pages, so a site can overrun by at most one page's fetch.
SITE_TIME_BUDGET = 60.0
# Timeouts and 5xx on consecutive pages mean the site is struggling; the rest
# of its pages would most likely fail the same slow way.
MAX_CONSECUTIVE_FAILURES = 2


@dataclass
class SiteCrawl:
    """The result of crawling one site."""

    domain: str
    start_url: str
    pages: list[Page] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # True when this site can never succeed (DNS, 404/403, robots-blocked), so
    # a later run should not spend a request on it again.
    permanently_failed: bool = False

    @property
    def pages_crawled(self) -> int:
        return len(self.pages)

    @property
    def business_name(self) -> str:
        """Best guess at the business name, from the homepage title."""
        if not self.pages:
            return ""
        title = self.pages[0].title
        # Titles are usually "Name — Tagline" or "Name | Tagline".
        for separator in ("|", "—", "–", " - ", "::"):
            if separator in title:
                return title.split(separator)[0].strip()
        return title.strip()

    @property
    def contacts(self) -> SiteContacts:
        return summarize([(p.url, p.contacts) for p in self.pages], self.domain)

    def thin_content(self, rules: Rules) -> bool:
        """True when the site yielded too little text to score reliably."""
        total = sum(len(p.text) for p in self.pages)
        return total < rules.thin_content_chars


def _top_domain(extracted) -> str:
    """tldextract renamed this property in 5.3; support both spellings."""
    value = getattr(extracted, "top_domain_under_public_suffix", None)
    if value is None:
        value = extracted.registered_domain
    return value.lower()


def registered_domain(url: str) -> str:
    """example.co.uk from https://www.example.co.uk/path — '' if unparseable."""
    return _top_domain(tldextract.extract(url))


def is_crawlable(url: str, rules: Rules) -> bool:
    """Reject platforms, directories, and non-business TLDs."""
    extracted = tldextract.extract(url)
    domain = _top_domain(extracted)

    if not domain:
        return False
    if domain in rules.domain_blocklist:
        return False
    if extracted.suffix.split(".")[-1] in rules.tld_blocklist:
        return False
    return True


def prioritize(links: list[str], base_domain: str, rules: Rules) -> list[str]:
    """Rank same-domain links by how likely they are to carry signal.

    Ordered by the crawl_priority list in signals.yaml (careers first, then
    news, about, contact, services); unclassified pages are dropped entirely
    rather than competing for the budget.
    """
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()

    priority = {name: i for i, name in enumerate(rules.crawl_priority)}

    for link in links:
        if registered_domain(link) != base_domain:
            continue
        if link in seen:
            continue
        seen.add(link)

        page_type = rules.page_type(link)
        if page_type not in priority:
            continue

        ranked.append((priority[page_type], link))

    ranked.sort(key=lambda pair: pair[0])
    return [link for _, link in ranked]


def crawl_site(
    start_url: str,
    fetcher: PoliteFetcher,
    rules: Rules,
    max_pages: int = DEFAULT_MAX_PAGES,
    time_budget: float = SITE_TIME_BUDGET,
) -> SiteCrawl:
    """Fetch a site's homepage plus its highest-value internal pages."""
    started = time.monotonic()
    if not start_url.startswith(("http://", "https://")):
        start_url = f"https://{start_url}"

    domain = registered_domain(start_url)
    crawl = SiteCrawl(domain=domain, start_url=start_url)

    home = fetcher.get(start_url)
    if not home.ok:
        crawl.errors.append(f"{start_url}: {home.error or home.status}")
        crawl.permanently_failed = getattr(home, "permanent", False)
        return crawl

    crawl.pages.append(build_page(home.url, home.html, rules.page_type(home.url)))

    if max_pages <= 1:
        return crawl

    candidates = prioritize(extract_links(home.html, start_url), domain, rules)

    to_fetch = candidates[: max_pages - 1]

    # The contact page carries little scoring signal, so on a site with many
    # careers and news links the budget used to run out before reaching it.
    # It's where the email and phone live, so it always gets a slot — first,
    # so the time budget can't cut it either.
    contact = next((c for c in candidates if rules.page_type(c) == "contact"), None)
    if contact and to_fetch:
        if contact in to_fetch:
            to_fetch.remove(contact)
        else:
            to_fetch.pop()
        to_fetch.insert(0, contact)

    consecutive_failures = 0

    for index, link in enumerate(to_fetch):
        skipped = len(to_fetch) - index
        if time.monotonic() - started > time_budget:
            # First, so it survives the CSV's three-error truncation.
            crawl.errors.insert(
                0, f"stopped: over {time_budget:g}s time budget, skipped {skipped} pages"
            )
            break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            crawl.errors.insert(
                0, f"stopped: {consecutive_failures} slow/failed pages in a row, "
                f"skipped {skipped} pages"
            )
            break

        result = fetcher.get(link)
        if not result.ok:
            crawl.errors.append(f"{link}: {result.error or result.status}")
            # A 404 or robots block is a fact about one page, not a sign the
            # site is slow, so only transient failures count toward giving up.
            if not getattr(result, "permanent", False):
                consecutive_failures += 1
            continue
        consecutive_failures = 0
        crawl.pages.append(
            build_page(result.url, result.html, rules.page_type(result.url))
        )

    log.info(
        "crawled %s: %d pages, %d errors", domain, crawl.pages_crawled, len(crawl.errors)
    )
    return crawl
