"""Proxy manager tests, including a REAL local HTTP CONNECT proxy to prove httpx goes through it."""
from __future__ import annotations

import select
import socket
import threading

import pytest

from drop_monitor.fetcher import Fetcher
from drop_monitor.proxies import ProxyEntry, ProxyFormatError, enabled_urls, entries_from_config, mask_proxy, parse_proxy_line, parse_proxy_list
from drop_monitor.proxies import test_proxy as check_proxy


@pytest.mark.parametrize("line,expected", [
    ("http://u:p@1.2.3.4:8080", "http://u:p@1.2.3.4:8080"),
    ("socks5://1.2.3.4:1080", "socks5://1.2.3.4:1080"),
    ("1.2.3.4:8080", "http://1.2.3.4:8080"),
    ("1.2.3.4:8080:user:pa ss", "http://user:pa%20ss@1.2.3.4:8080"),
    ("user:p@ss@proxy.example.com:3128", "http://user:p%40ss@proxy.example.com:3128"),
])
def test_parse_formats(line, expected):
    assert parse_proxy_line(line) == expected


def test_parse_errors_and_list():
    for bad in ["", "#c", "nope", "1.2.3.4", "1.2.3.4:99999", "ftp://x:1"]:
        with pytest.raises(ProxyFormatError):
            parse_proxy_line(bad)
    urls, errors = parse_proxy_list("1.2.3.4:80\n# comment\n\nbad line here\n1.2.3.4:80\n", "socks5")
    assert urls == ["socks5://1.2.3.4:80"] and len(errors) == 1
    assert mask_proxy("http://alice:secret@h:1") == "http://al***@h:1" and mask_proxy("http://h:1") == "http://h:1"
    entries = entries_from_config(["http://a:1", {"url": "http://b:2", "group": "resi", "enabled": False}, {"url": "http://c:3", "group": "resi"}])
    assert [e.group for e in entries] == ["default", "resi", "resi"]
    assert enabled_urls(entries) == ["http://a:1", "http://c:3"] and enabled_urls(entries, "resi") == ["http://c:3"]
    assert ProxyEntry("http://a:1").to_config() == "http://a:1" and ProxyEntry("http://b:2", "resi", False).to_config() == {"url": "http://b:2", "group": "resi", "enabled": False}


# ---------------------------------------------------------------- real local proxy
class LocalConnectProxy(threading.Thread):
    """Minimal HTTP proxy: handles CONNECT (tunnels) and plain GET (answers itself). Counts requests."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.hits: list[str] = []
        self._stop = False

    def run(self):
        while not self._stop:
            r, _, _ = select.select([self.sock], [], [], 0.2)
            if not r:
                continue
            conn, _ = self.sock.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = conn.recv(65535)
            first = data.split(b"\r\n", 1)[0].decode(errors="ignore")
            self.hits.append(first)
            if first.startswith("CONNECT"):
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")  # no TLS in the test
            else:
                body = b"<html><body>via local proxy</body></html>"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        finally:
            conn.close()

    def stop(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def local_proxy():
    p = LocalConnectProxy()
    p.start()
    yield p
    p.stop()


def test_requests_really_go_through_the_proxy(local_proxy):
    url = f"http://127.0.0.1:{local_proxy.port}"
    f = Fetcher("ua", proxies=[url], proxy_mode="rotate", respect_robots=False)
    r = f.fetch("http://example.invalid/page")  # plain-HTTP request is forwarded to the proxy, which answers
    f.close()
    assert "via local proxy" in r.text
    assert any(h.startswith("GET http://example.invalid/page") for h in local_proxy.hits)
    res = check_proxy(url, "http://example.invalid/", timeout=5, ip_service=None)
    assert res.ok and res.latency_ms is not None and res.latency_ms >= 0
    res_https = check_proxy(url, "https://example.invalid/", timeout=5, ip_service=None)
    assert not res_https.ok and "403" in (res_https.error or "")  # CONNECT refused by our fake proxy -> reported, not crashed
    assert any(h.startswith("CONNECT example.invalid:443") for h in local_proxy.hits)


def test_dead_proxy_is_reported_not_raised():
    res = check_proxy("http://127.0.0.1:9", "http://example.invalid/", timeout=3, ip_service=None)
    assert not res.ok and res.error


def test_socks_scheme_is_supported_by_the_installed_httpx():
    import httpx

    httpx.Client(proxy="socks5://127.0.0.1:1080").close()  # would raise ImportError without socksio
