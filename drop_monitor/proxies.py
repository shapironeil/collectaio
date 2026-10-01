"""Proxy manager (AIO style): parse any common format, groups, enable/disable, mass test with latency and seen IP.

Entries live in config.yaml under network.proxies as strings or mappings:
  - "http://user:pass@host:port"
  - {url: "socks5://host:1080", group: "resi", enabled: true}
Accepted input formats when importing: scheme://[user:pass@]host:port, host:port, host:port:user:pass, user:pass@host:port.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

_SCHEMES = ("http", "https", "socks5", "socks5h")


class ProxyFormatError(ValueError):
    pass


@dataclass
class ProxyEntry:
    url: str
    group: str = "default"
    enabled: bool = True
    label: str = ""

    def masked(self) -> str:
        return mask_proxy(self.url)

    def to_config(self) -> dict | str:
        if self.group == "default" and self.enabled and not self.label:
            return self.url
        d = {"url": self.url, "group": self.group, "enabled": self.enabled}
        if self.label:
            d["label"] = self.label
        return d


@dataclass
class TestResult:
    url: str
    ok: bool
    latency_ms: int | None = None
    ip: str | None = None
    error: str | None = None
    tested_at: float = field(default_factory=time.time)


def parse_proxy_line(line: str, default_scheme: str = "http") -> str:
    """Normalise one proxy in any common format to a URL. Raises ProxyFormatError."""
    s = line.strip()
    if not s or s.startswith("#"):
        raise ProxyFormatError("riga vuota")
    if "://" in s:
        u = urlsplit(s)
        if u.scheme not in _SCHEMES or not u.hostname or not u.port:
            raise ProxyFormatError(f"proxy non valido: {s}")
        return s
    if "@" in s:  # user:pass@host:port
        creds, hostport = s.rsplit("@", 1)
        host, port = _hostport(hostport)
        user, _, pw = creds.partition(":")
        return f"{default_scheme}://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"
    parts = s.split(":")
    if len(parts) == 2:
        host, port = _hostport(s)
        return f"{default_scheme}://{host}:{port}"
    if len(parts) == 4:  # host:port:user:pass
        host, port = _hostport(parts[0] + ":" + parts[1])
        return f"{default_scheme}://{quote(parts[2], safe='')}:{quote(parts[3], safe='')}@{host}:{port}"
    raise ProxyFormatError(f"formato non riconosciuto: {s}")


def _hostport(hp: str) -> tuple[str, int]:
    host, _, port = hp.rpartition(":")
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise ProxyFormatError(f"host:porta non valido: {hp}")
    return host, int(port)


def parse_proxy_list(text: str, default_scheme: str = "http") -> tuple[list[str], list[str]]:
    """Returns (urls, errors) for a multi-line import."""
    urls, errors = [], []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        try:
            u = parse_proxy_line(line, default_scheme)
            if u not in urls:
                urls.append(u)
        except ProxyFormatError as e:
            errors.append(str(e))
    return urls, errors


def mask_proxy(url: str) -> str:
    u = urlsplit(url)
    if u.username:
        netloc = f"{u.username[:2]}***@{u.hostname}:{u.port}"
        return urlunsplit((u.scheme, netloc, "", "", ""))
    return url


def entries_from_config(items) -> list[ProxyEntry]:
    out: list[ProxyEntry] = []
    for it in items or []:
        if isinstance(it, str):
            if it.strip():
                out.append(ProxyEntry(url=it.strip()))
        elif isinstance(it, dict) and it.get("url"):
            out.append(ProxyEntry(url=str(it["url"]).strip(), group=str(it.get("group") or "default"), enabled=bool(it.get("enabled", True)), label=str(it.get("label") or "")))
    return out


def enabled_urls(entries: list[ProxyEntry], group: str = "") -> list[str]:
    return [e.url for e in entries if e.enabled and (not group or e.group == group)]


def test_proxy(url: str, target: str, timeout: float = 12.0, ip_service: str | None = "https://api.ipify.org") -> TestResult:
    """One real request through the proxy to `target` (HEAD/GET), plus the IP the world sees (optional)."""
    t0 = time.monotonic()
    try:
        with httpx.Client(proxy=url, timeout=httpx.Timeout(timeout), follow_redirects=True, headers={"User-Agent": "collectaio-proxy-test"}) as c:
            r = c.get(target)
            latency = int((time.monotonic() - t0) * 1000)
            if r.status_code >= 500:
                return TestResult(url, False, latency, None, f"HTTP {r.status_code} dal sito")
            ip = None
            if ip_service:
                try:
                    ip = c.get(ip_service, timeout=timeout).text.strip()[:64]
                except httpx.HTTPError:
                    ip = None
            return TestResult(url, True, latency, ip, None)
    except httpx.ProxyError as e:
        return TestResult(url, False, None, None, f"proxy rifiuta: {e}")
    except httpx.HTTPError as e:
        return TestResult(url, False, None, None, f"{type(e).__name__}: {e}")
    except Exception as e:  # pragma: no cover - e.g. unsupported scheme
        return TestResult(url, False, None, None, f"{type(e).__name__}: {e}")


def test_many(urls: list[str], target: str, timeout: float = 12.0, workers: int = 8, ip_service: str | None = "https://api.ipify.org") -> list[TestResult]:
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1))) as ex:
        return list(ex.map(lambda u: test_proxy(u, target, timeout, ip_service), urls))


class ResultStore:
    """Last test result per proxy, persisted to data/proxy-tests.json."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.results: dict[str, dict] = {}
        if self.path.is_file():
            try:
                self.results = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self.results = {}

    def update(self, results: list[TestResult]) -> None:
        for r in results:
            self.results[r.url] = asdict(r)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.results, indent=1), encoding="utf-8")

    def get(self, url: str) -> dict | None:
        return self.results.get(url)
