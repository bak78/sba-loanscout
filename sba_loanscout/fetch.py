"""Polite HTTP fetching: robots.txt, per-host rate limiting, retries, caps.

The per-host delay is enforced with a lock keyed by hostname, so it holds even
though the CLI crawls many domains concurrently. Workers are spread across
domains precisely so this serialization is never the bottleneck.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import requests

log = logging.getLogger(__name__)

DEFAULT_DELAY = 1.0
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 10
MAX_RETRIES = 2
# TIMEOUT only fires when a server goes silent. A server that trickles bytes
# never trips it, so the download itself also gets a wall-clock limit.
PAGE_DEADLINE = 20
# A robots.txt Crawl-delay above this makes one site cost minutes. Such sites
# are skipped rather than crawled faster than they asked.
MAX_CRAWL_DELAY = 10.0
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Statuses that will not change on a later run. Sites that block crawlers
# commonly answer 403 or a soft 404, and retrying them every run is what stops
# a destination list from ever draining.
PERMANENT_STATUSES = {400, 401, 403, 404, 405, 410, 451}


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    html: str
    error: str = ""
    permanent: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


def _is_dns_failure(exc: BaseException) -> bool:
    """True when a request failed because the hostname does not resolve.

    requests wraps this several layers deep, so the exception chain is walked
    rather than matched on the top-level type.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 10:
        if type(current).__name__ in ("NameResolutionError", "gaierror"):
            return True
        current = current.__context__ or current.__cause__
        seen += 1
    return False


def _chunks_as_they_arrive(response: requests.Response):
    """Yield body bytes as soon as any arrive.

    iter_content(8192) blocks until a full 8 KB is buffered, so a server
    trickling a few bytes at a time would never let the deadline be checked.
    read1 returns whatever is already there.
    """
    raw = response.raw
    if not hasattr(raw, "read1"):  # urllib3 1.x
        yield from response.iter_content(8192)
        return
    while True:
        chunk = raw.read1(8192, decode_content=True)
        if not chunk:
            return
        yield chunk


class PoliteFetcher:
    """A requests session that respects robots.txt and rate limits per host."""

    def __init__(self, user_agent: str, default_delay: float = DEFAULT_DELAY):
        self.user_agent = user_agent
        self.default_delay = default_delay

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._delays: dict[str, float] = {}
        self._last_request: dict[str, float] = {}

        self._robots_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()

        # Set on Ctrl+C so workers stop waiting and return instead of finishing
        # their site's remaining pages.
        self._stopped = threading.Event()

    # -- robots ---------------------------------------------------------

    def _host_lock(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _robots_for(self, url: str):
        """Fetch and cache robots.txt for a host. None means 'no rules found'."""
        parsed = urlparse(url)
        host = parsed.netloc

        with self._robots_lock:
            if host in self._robots:
                return self._robots[host]

        robots_url = urlunparse((parsed.scheme, host, "/robots.txt", "", "", ""))
        parser: urllib.robotparser.RobotFileParser | None = None

        try:
            response = self._session.get(robots_url, timeout=TIMEOUT)
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
                delay = parser.crawl_delay(self.user_agent)
                if delay:
                    self._delays[host] = max(float(delay), self.default_delay)
        except requests.RequestException as exc:
            # A missing or unreachable robots.txt means no stated restrictions.
            log.debug("robots.txt unavailable for %s: %s", host, exc)
        finally:
            # This was a real request to the host, so it starts the delay clock.
            # Without this the robots fetch and the first page arrive back to
            # back, which is exactly the burst the delay exists to prevent.
            self._last_request[host] = time.monotonic()

        with self._robots_lock:
            self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # -- rate limiting --------------------------------------------------

    def _wait_turn(self, host: str) -> None:
        delay = self._delays.get(host, self.default_delay)
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                self._stopped.wait(remaining)
        self._last_request[host] = time.monotonic()

    # -- fetching -------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        """Fetch one URL, honoring robots.txt and the per-host delay."""
        if self._stopped.is_set():
            return FetchResult(url, 0, "", "interrupted")
        if not self.allowed(url):
            return FetchResult(url, 0, "", "blocked by robots.txt", permanent=True)

        host = urlparse(url).netloc

        delay = self._delays.get(host, self.default_delay)
        if delay > MAX_CRAWL_DELAY:
            return FetchResult(
                url,
                0,
                "",
                f"robots.txt crawl-delay {delay:g}s exceeds {MAX_CRAWL_DELAY:g}s limit",
                permanent=True,
            )

        # Held across the request so two workers never overlap on one host.
        with self._host_lock(host):
            self._wait_turn(host)
            if self._stopped.is_set():
                return FetchResult(url, 0, "", "interrupted")
            return self._get_with_retries(url)

    def _get_with_retries(self, url: str) -> FetchResult:
        last_error = ""

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._session.get(
                    url, timeout=TIMEOUT, stream=True, allow_redirects=True
                )

                if response.status_code in RETRY_STATUSES:
                    response.close()
                    last_error = f"http {response.status_code}"
                    if attempt < MAX_RETRIES and not self._stopped.wait(2**attempt):
                        continue
                    return FetchResult(url, response.status_code, "", last_error)

                # Status first: a 404 served with no Content-Type is a 404,
                # not a content-type problem, and saying so makes the CSV's
                # crawl_errors column tell the truth about why a site failed.
                if response.status_code != 200:
                    response.close()
                    return FetchResult(
                        url,
                        response.status_code,
                        "",
                        f"http {response.status_code}",
                        permanent=response.status_code in PERMANENT_STATUSES,
                    )

                # A missing Content-Type is common on misconfigured servers;
                # attempt the parse rather than discarding a live page.
                content_type = response.headers.get("Content-Type")
                if content_type and "html" not in content_type.lower():
                    response.close()
                    return FetchResult(
                        url,
                        response.status_code,
                        "",
                        f"not html ({content_type})",
                        permanent=True,
                    )

                body = self._read_capped(response)
                return FetchResult(url, 200, body)

            except requests.RequestException as exc:
                if _is_dns_failure(exc):
                    # The host does not resolve. No number of retries fixes
                    # that, and no later run will either.
                    return FetchResult(
                        url, 0, "", "dns: host does not resolve", permanent=True
                    )
                last_error = type(exc).__name__
                if attempt < MAX_RETRIES and not self._stopped.wait(2**attempt):
                    continue
                break

        return FetchResult(url, 0, "", last_error or "request failed")

    @staticmethod
    def _read_capped(response: requests.Response) -> str:
        """Read at most MAX_BYTES for at most PAGE_DEADLINE seconds, so one huge
        or slow-trickling page can't stall a run. A cut-off page keeps what
        arrived — the homepage text usually comes first."""
        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + PAGE_DEADLINE
        for chunk in _chunks_as_they_arrive(response):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
            if time.monotonic() > deadline:
                log.debug("%s: stopped reading after %ss", response.url, PAGE_DEADLINE)
                break
        response.close()

        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")

    def stop(self) -> None:
        """Make every in-progress and future get() return quickly."""
        self._stopped.set()

    def close(self) -> None:
        self._session.close()
