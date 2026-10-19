"""Politeness tests. These run against a local server, never the internet."""

import gzip
import http.server
import socketserver
import threading
import time

import pytest

from sba_loanscout import fetch
from sba_loanscout.fetch import PoliteFetcher

ROBOTS = b"User-agent: *\nDisallow: /private/\n"

PAGE = b"<html><head><title>T</title></head><body><p>hello</p></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []
    robots = ROBOTS

    def do_GET(self):
        type(self).hits.append((time.monotonic(), self.path))
        if self.path == "/robots.txt":
            self._send(200, "text/plain", type(self).robots)
        elif self.path == "/trickle":
            # A server that never goes silent long enough to trip TIMEOUT.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"<p>slow</p>")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass  # the client hung up, which is the point
        elif self.path == "/gzip":
            body = gzip.compress(PAGE)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/notfound":
            self.send_error(404)
        elif self.path == "/image":
            self._send(200, "image/png", b"\x89PNG")
        else:
            self._send(200, "text/html; charset=utf-8", PAGE)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.hits = []
    _Handler.robots = ROBOTS
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _Handler
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fetcher():
    f = PoliteFetcher("LoanScoutBot/0.1 (+test@example.com)", default_delay=0.3)
    yield f
    f.close()


def test_fetches_html_successfully(server, fetcher):
    base, _ = server
    result = fetcher.get(f"{base}/")
    assert result.ok
    assert "hello" in result.html


def test_robots_disallow_is_honored(server, fetcher):
    base, handler = server
    result = fetcher.get(f"{base}/private/secret")

    assert not result.ok
    assert result.error == "blocked by robots.txt"
    assert not any(p.startswith("/private") for _, p in handler.hits)


def test_robots_fetch_starts_the_delay_clock(server, fetcher):
    """Regression: robots.txt and the first page must not arrive back to back."""
    base, handler = server
    fetcher.get(f"{base}/")

    times = [t for t, _ in handler.hits]
    assert len(times) == 2, "expected a robots.txt fetch then a page fetch"
    assert times[1] - times[0] >= 0.25, "page followed robots.txt with no delay"


def test_per_host_delay_between_pages(server, fetcher):
    base, handler = server
    for path in ("/a", "/b", "/c"):
        fetcher.get(f"{base}{path}")

    page_times = [t for t, p in handler.hits if p != "/robots.txt"]
    gaps = [b - a for a, b in zip(page_times, page_times[1:])]
    assert all(g >= 0.25 for g in gaps), f"gaps too short: {gaps}"


def test_concurrent_requests_to_one_host_stay_serialized(server, fetcher):
    """The per-host lock must hold even with many workers on one domain."""
    base, handler = server
    fetcher.get(f"{base}/warm")  # prime the robots cache

    threads = [
        threading.Thread(target=fetcher.get, args=(f"{base}/p{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    page_times = sorted(t for t, p in handler.hits if p != "/robots.txt")
    gaps = [b - a for a, b in zip(page_times, page_times[1:])]
    assert all(g >= 0.25 for g in gaps), f"concurrent requests burst: {gaps}"


def test_non_html_is_skipped(server, fetcher):
    base, _ = server
    result = fetcher.get(f"{base}/image")
    assert not result.ok
    assert "not html" in result.error


def test_404_is_reported_not_retried(server, fetcher):
    base, handler = server
    result = fetcher.get(f"{base}/notfound")

    assert not result.ok
    assert result.status == 404
    assert sum(1 for _, p in handler.hits if p == "/notfound") == 1


def test_long_crawl_delay_skips_the_site(server, fetcher):
    """A 30s Crawl-delay would cost minutes per site; skip rather than wait."""
    base, handler = server
    handler.robots = b"User-agent: *\nCrawl-delay: 30\n"

    started = time.monotonic()
    result = fetcher.get(f"{base}/")

    assert not result.ok
    assert result.permanent
    assert "crawl-delay" in result.error
    assert time.monotonic() - started < 5
    assert [p for _, p in handler.hits] == ["/robots.txt"], "page must not be fetched"


def test_modest_crawl_delay_is_still_honored(server, fetcher):
    base, handler = server
    handler.robots = b"User-agent: *\nCrawl-delay: 1\n"
    for path in ("/a", "/b"):
        assert fetcher.get(f"{base}{path}").ok

    page_times = [t for t, p in handler.hits if p != "/robots.txt"]
    assert page_times[1] - page_times[0] >= 0.95


def test_slow_trickling_page_is_cut_off(server, fetcher, monkeypatch):
    """TIMEOUT never fires on a server that keeps sending; the deadline must."""
    monkeypatch.setattr(fetch, "PAGE_DEADLINE", 0.5)
    base, _ = server

    started = time.monotonic()
    result = fetcher.get(f"{base}/trickle")
    elapsed = time.monotonic() - started

    assert elapsed < 2.5, f"deadline ignored, took {elapsed:.1f}s"
    assert result.ok, "a cut-off page keeps the text that arrived"
    assert "slow" in result.html


def test_gzip_pages_are_decoded(server, fetcher):
    base, _ = server
    result = fetcher.get(f"{base}/gzip")
    assert result.ok
    assert "hello" in result.html


def test_stopped_fetcher_returns_without_requesting(server, fetcher):
    base, handler = server
    fetcher.stop()

    result = fetcher.get(f"{base}/")

    assert not result.ok
    assert result.error == "interrupted"
    assert not result.permanent, "an interrupted site must be retried next run"
    assert handler.hits == []
