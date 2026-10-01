"""Polite HTTP client: honest User-Agent, robots.txt, timeouts, classified errors."""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from drop_monitor.models import FetchResult

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised for non-2xx responses and transport errors."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.url = url

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_server_error(self) -> bool:
        return self.status is not None and 500 <= self.status < 600

    @property
    def is_not_found(self) -> bool:
        return self.status in (404, 410)

    @property
    def should_backoff(self) -> bool:
        return self.status is None or self.is_rate_limit or self.is_server_error or self.status == 403


class RobotsDisallowed(Exception):
    pass


class RobotsCache:
    """Per-host robots.txt with a TTL. Fail-open on fetch errors (logged)."""

    def __init__(self, client: httpx.Client, user_agent: str, ttl_seconds: float = 24 * 3600):
        self._client = client
        self._ua = user_agent
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        now = time.monotonic()
        entry = self._cache.get(host)
        if entry is None or now - entry[0] > self._ttl:
            entry = (now, self._load(url))
            self._cache[host] = entry
        rp = entry[1]
        if rp is None:
            return True
        return rp.can_fetch(self._ua, url)

    def crawl_delay(self, url: str) -> float | None:
        entry = self._cache.get(urlparse(url).netloc)
        if entry and entry[1] is not None:
            try:
                return entry[1].crawl_delay(self._ua)
            except Exception:  # pragma: no cover - defensive
                return None
        return None

    def _load(self, url: str):
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self._client.get(robots_url)
        except httpx.HTTPError as e:
            log.warning("robots.txt fetch failed (%s): assuming allowed", e)
            return None
        if r.status_code >= 400:
            log.info("robots.txt %s -> HTTP %s: assuming allowed", robots_url, r.status_code)
            rp.parse([])
            return rp
        rp.parse(r.text.splitlines())
        log.info("robots.txt loaded from %s", robots_url)
        return rp


class Fetcher:
    def __init__(self, user_agent: str, timeout: float = 20, accept_language: str = "it-IT,it;q=0.9,en;q=0.5", respect_robots: bool = True,
                 proxies: list[str] | None = None, proxy_mode: str = "off"):
        self.user_agent = user_agent
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
            "Accept-Language": accept_language,
        }
        proxy_list = list(proxies or []) if proxy_mode != "off" else []
        self._clients = [
            httpx.Client(headers=headers, timeout=httpx.Timeout(timeout), follow_redirects=True, proxy=px)
            for px in (proxy_list or [None])
        ]
        self.proxies = proxy_list
        self._rr = 0
        self._client = self._clients[0]
        self.respect_robots = respect_robots
        self.robots = RobotsCache(self._client, user_agent)

    def _next_client(self) -> httpx.Client:
        if len(self._clients) == 1:
            return self._clients[0]
        c = self._clients[self._rr % len(self._clients)]
        self._rr += 1
        return c

    def close(self) -> None:
        for c in self._clients:
            c.close()

    def fetch(self, url: str) -> FetchResult:
        if self.respect_robots and not self.robots.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url} for '{self.user_agent}'")
        t0 = time.monotonic()
        try:
            r = self._next_client().get(url)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}", status=None, url=url) from e
        elapsed = time.monotonic() - t0
        if r.status_code >= 400:
            retry_after = _parse_retry_after(r.headers.get("Retry-After"))
            raise FetchError(f"HTTP {r.status_code} for {url}", status=r.status_code, retry_after=retry_after, url=url)
        return FetchResult(url=str(r.url), status=r.status_code, text=r.text, content_type=r.headers.get("Content-Type", ""), elapsed=elapsed)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return max(0.0, (parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError):
            return None
