"""End-to-end scenarios with a fake fetcher serving the fixtures (no network)."""
from __future__ import annotations

from pathlib import Path

import pytest

from drop_monitor.config import Config, NotifyConfig, PollingConfig, Source, StorageConfig, TelegramConfig
from drop_monitor.fetcher import FetchError, RobotsDisallowed
from drop_monitor.matching import Watch
from drop_monitor.models import FetchResult, State
from drop_monitor.scheduler import Monitor
from drop_monitor.store import Store

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.gemcardinfinitycollection.it"
CAT = f"{BASE}/it/pokemon-30%C2%BA-anniversario"
TIN_URL = f"{BASE}/it/pokemon-30-anniversario-mini-tin-case-sealed-ita"


class FakeRobots:
    def crawl_delay(self, url):
        return None


class FakeFetcher:
    """Maps URL -> fixture name | FetchError | RobotsDisallowed."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []
        self.robots = FakeRobots()

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            raise FetchError(f"HTTP 404 for {url}", status=404, url=url)
        if isinstance(route, Exception):
            raise route
        ctype = "application/rss+xml" if route.endswith(".xml") else "text/html"
        return FetchResult(url=url, status=200, text=(FIX / route).read_text(encoding="utf-8"), content_type=ctype)


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, **kw):
        self.sent.append(text)
        return True


def _cfg(tmp_path, sources, watches, hot_ratio=2, track=True):
    return Config(
        sources=sources,
        watches=watches,
        track_product_pages=track,
        polling=PollingConfig(min_seconds=1, max_seconds=1, backoff_base_seconds=10, backoff_max_seconds=100, hot_ratio=hot_ratio),
        telegram=TelegramConfig(enabled=False),
        notify=NotifyConfig(on_price_change=True, startup_message=False, error_after_consecutive=2),
        storage=StorageConfig(db_path=":memory:", log_path=None, health_path=str(tmp_path / "health.json")),
    )


def test_category_discovery_then_product_page_restock(tmp_path):
    # Page 1 of the category is the (page-2) fixture; no other pages exist (pager links 404 -> backoff),
    # so we point the source at the pagenumber=2 URL whose pager "others" are 1,3,4.
    cat2 = f"{CAT}?pagenumber=2"
    routes = {cat2: "nop_category_page2_all_oos.html", TIN_URL: "nop_product_oos.html"}
    cfg = _cfg(tmp_path, [Source(url=cat2, type="category", follow_pagination=False)], [Watch(keywords="Pokemon 30 Anniversario Mini Tin Case ITA", exclude=["casuale"])])
    fetcher, tg = FakeFetcher(routes), FakeTelegram()
    m = Monitor(cfg, Store(":memory:"), fetcher, tg)

    r1 = m.step()  # no known URL yet -> discovery first
    assert r1.kind == "discovery" and r1.products == 6
    assert [t.new_state for t in r1.transitions if t.changed] == [State.PRESENT_UNAVAILABLE]
    assert len(tg.sent) == 1 and "comparso" in tg.sent[0] and "€129,89" in tg.sent[0]

    r2 = m.step()  # URL now known -> hot product page
    assert r2.kind == "hot" and r2.url == TIN_URL
    assert not any(t.changed for t in r2.transitions)
    assert len(tg.sent) == 1  # unchanged: no notification

    fetcher.routes[TIN_URL] = "nop_product_available.html"  # restock! (title differs but URL is trusted)
    fetcher.routes[cat2] = "nop_category_page2_minitin_available.html"  # the listing agrees
    r3 = m.step()
    assert r3.kind == "hot"
    assert [t.new_state for t in r3.transitions if t.changed] == [State.AVAILABLE]
    assert "DISPONIBILE ORA" in tg.sent[-1] and "addproducttocart/details/3757/1" in tg.sent[-1]

    # weighted round robin: hot, hot, discovery, hot, hot, ...
    kinds = [m.step().kind for _ in range(4)]
    assert kinds == ["discovery", "hot", "hot", "discovery"]
    assert m.store.get_tracked(m.cfg.watches[0].keywords.strip().lower()).state == State.AVAILABLE


def test_product_page_404_marks_absent_and_restock_from_absent(tmp_path):
    cfg = _cfg(tmp_path, [], [Watch(keywords="Mini Tin Case", url=TIN_URL)])
    fetcher, tg = FakeFetcher({TIN_URL: "nop_product_oos.html"}), FakeTelegram()
    m = Monitor(cfg, Store(":memory:"), fetcher, tg)
    assert m.step().transitions[0].new_state == State.PRESENT_UNAVAILABLE
    del fetcher.routes[TIN_URL]
    r = m.step()
    assert r.ok and r.transitions[0].new_state == State.ABSENT and m.consecutive_errors == 0
    assert "rimosso" in tg.sent[-1]
    fetcher.routes[TIN_URL] = "nop_product_available.html"
    assert m.step().transitions[0].new_state == State.AVAILABLE
    assert "NUOVO E DISPONIBILE" in tg.sent[-1]


def test_backoff_on_429_and_5xx_and_recovery(tmp_path):
    cfg = _cfg(tmp_path, [], [Watch(keywords="Mini Tin Case", url=TIN_URL)])
    fetcher, tg = FakeFetcher({TIN_URL: FetchError("HTTP 429", status=429, retry_after=50, url=TIN_URL)}), FakeTelegram()
    m = Monitor(cfg, Store(":memory:"), fetcher, tg)
    import time

    r = m.step()
    assert not r.ok and m.consecutive_errors == 1
    assert m.backoff_until - time.time() >= 49  # Retry-After honoured over base backoff
    fetcher.routes[TIN_URL] = FetchError("HTTP 503", status=503, url=TIN_URL)
    m.step()
    assert m.consecutive_errors == 2
    assert 19 <= m.backoff_until - time.time() <= 26  # 10 * 2^1 with up to 25% jitter
    assert any("errori consecutivi" in s for s in tg.sent)
    assert m.interval() >= 19
    fetcher.routes[TIN_URL] = "nop_product_oos.html"
    m.step()
    assert m.consecutive_errors == 0 and m.backoff_until == 0
    assert any("risponde di nuovo" in t for t in tg.sent)
    assert m.interval() == 1


def test_robots_disallow_disables_discovery_source(tmp_path):
    search = f"{BASE}/it/SmartSearch/Search?q=pokemon"
    cfg = _cfg(tmp_path, [Source(url=search, type="search")], [Watch(keywords="Mini Tin Case")])
    fetcher, tg = FakeFetcher({search: RobotsDisallowed(f"robots.txt disallows {search}")}), FakeTelegram()
    m = Monitor(cfg, Store(":memory:"), fetcher, tg)
    r = m.step()
    assert not r.ok and m.discovery[0].disabled_reason
    assert m.step().kind == "idle"
    assert "robots" in tg.sent[0]


def test_paginated_sweep_marks_missing_product_absent(tmp_path):
    """Product seen on page 2 of a listing, then gone on a later complete sweep (no hot page)."""
    page1, page2 = f"{CAT}", f"{CAT}?pagenumber=2"
    # fixture "page2" declares pages 1,3,4 in its pager; pages 3/4 get a page with no products and no pager.
    routes = {page1: "nop_category_page2_all_oos.html", page2: "nop_category_page2_all_oos.html",
              f"{CAT}?pagenumber=3": "nop_product_oos.html", f"{CAT}?pagenumber=4": "nop_product_oos.html"}
    cfg = _cfg(tmp_path, [Source(url=page1, type="category")], [Watch(keywords="Bundle 6 Buste")], track=False)
    fetcher, tg = FakeFetcher(routes), FakeTelegram()
    m = Monitor(cfg, Store(":memory:"), fetcher, tg)
    steps = [m.step() for _ in range(3)]  # one full sweep: page1 (which is "page 2" in the fixture) + pages 3, 4
    assert m.discovery[0].sweeps_completed == 1 and not m.discovery[0].pending_pages
    assert steps[0].transitions[0].new_state == State.PRESENT_UNAVAILABLE
    # second sweep: bundle no longer listed anywhere (page 1 now has no products and no pager)
    fetcher.routes[page1] = "nop_product_oos.html"
    steps = [m.step() for _ in range(1)]
    assert m.discovery[0].sweeps_completed == 2
    absent = [t for s in steps for t in s.transitions if t.new_state == State.ABSENT]
    assert len(absent) == 1 and "rimosso" in tg.sent[-1]


def test_run_once_sweeps_everything(tmp_path):
    routes = {f"{BASE}/newproducts/rss": "nop_newproducts.xml", TIN_URL: "nop_product_oos.html"}
    cfg = _cfg(tmp_path, [Source(url=f"{BASE}/newproducts/rss", type="rss")], [Watch(keywords="Mini Tin Case", url=TIN_URL), Watch(keywords="Storm Emerald Display")])
    m = Monitor(cfg, Store(":memory:"), FakeFetcher(routes), FakeTelegram(), dry_run=True)
    results = m.run_once(delay=0)
    kinds = sorted(r.kind for r in results)
    assert kinds == ["discovery", "hot"] or kinds == ["discovery", "hot", "hot"]
    assert (tmp_path / "health.json").exists()
    storm = m.store.get_tracked("storm emerald display")
    assert storm.state == State.PRESENT_UNAVAILABLE and storm.url.endswith("30-buste-jap")
