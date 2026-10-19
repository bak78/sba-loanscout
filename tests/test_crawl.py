"""Crawl bounds: a site costs a fixed, small number of requests, and slow or
failing sites are abandoned rather than allowed to stall a run."""

import pytest

from sba_loanscout.crawl import crawl_site, prioritize


# --- the crawl genuinely has no frontier ------------------------------------


def test_crawl_never_exceeds_page_budget(rules):
    """Links found on fetched pages are never enqueued, so depth is fixed."""
    calls = []

    class StubFetcher:
        def get(self, url):
            calls.append(url)
            from sba_loanscout.fetch import FetchResult

            # Every page links to ten more, all of them crawl-priority pages.
            links = "".join(
                f'<a href="/careers/{len(calls)}-{i}">j</a>' for i in range(10)
            )
            return FetchResult(url, 200, f"<html><body>{links}</body></html>")

    crawl = crawl_site("https://example.com/", StubFetcher(), rules, max_pages=4)

    assert crawl.pages_crawled == 4
    assert len(calls) == 4, "a frontier would have kept expanding"


def _site_stub(subpage_result):
    """A fetcher whose homepage links to seven careers pages, each answered by
    subpage_result(url)."""
    from sba_loanscout.fetch import FetchResult

    calls = []
    links = "".join(f'<a href="/careers/{i}">j</a>' for i in range(7))

    class StubFetcher:
        def get(self, url):
            calls.append(url)
            if len(calls) == 1:
                return FetchResult(url, 200, f"<html><body>{links}</body></html>")
            return subpage_result(url)

    return StubFetcher(), calls


def test_site_time_budget_stops_the_crawl(rules):
    from sba_loanscout.fetch import FetchResult

    fetcher, calls = _site_stub(lambda url: FetchResult(url, 200, "<p>ok</p>"))
    crawl = crawl_site("https://example.com/", fetcher, rules, time_budget=0)

    assert len(calls) == 1, "only the homepage fits in a zero budget"
    assert crawl.pages_crawled == 1, "pages already fetched are kept and scored"
    assert crawl.errors[0].startswith("stopped: over 0s time budget, skipped 7")


def test_repeated_slow_failures_abandon_the_site(rules):
    from sba_loanscout.fetch import FetchResult

    fetcher, calls = _site_stub(lambda url: FetchResult(url, 0, "", "ReadTimeout"))
    crawl = crawl_site("https://example.com/", fetcher, rules)

    assert len(calls) == 3, "homepage plus two failures, then give up"
    assert crawl.pages_crawled == 1
    assert crawl.errors[0].startswith("stopped: 2 slow/failed pages in a row")


def test_permanent_page_failures_do_not_abandon_the_site(rules):
    """A 404 is a fact about one page, not a sign the site is slow."""
    from sba_loanscout.fetch import FetchResult

    fetcher, calls = _site_stub(
        lambda url: FetchResult(url, 404, "", "http 404", permanent=True)
    )
    crawl_site("https://example.com/", fetcher, rules)

    assert len(calls) == 8


def test_prioritize_drops_unclassified_pages(rules):
    links = [
        "https://example.com/careers",
        "https://example.com/privacy",
        "https://example.com/terms",
        "https://other.com/careers",
    ]
    kept = prioritize(links, "example.com", rules)

    assert kept == ["https://example.com/careers"]



@pytest.mark.parametrize(
    "status,permanent",
    [(404, True), (403, True), (410, True), (500, False), (503, False)],
)
def test_status_permanence_classification(status, permanent):
    from sba_loanscout.fetch import PERMANENT_STATUSES

    assert (status in PERMANENT_STATUSES) is permanent


def test_dns_failure_detected_through_exception_chain():
    from sba_loanscout.fetch import _is_dns_failure

    class NameResolutionError(Exception):
        pass

    try:
        try:
            raise NameResolutionError("nope")
        except NameResolutionError as inner:
            raise ConnectionError("wrapped") from inner
    except ConnectionError as exc:
        assert _is_dns_failure(exc)


def test_ordinary_timeout_is_not_permanent():
    from sba_loanscout.fetch import _is_dns_failure

    assert not _is_dns_failure(TimeoutError("slow"))
