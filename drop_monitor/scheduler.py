"""The polling loop: one HTTP request per cycle, jittered interval, exponential backoff.

Two request queues are served in a weighted round-robin:

* **hot**: product pages of watched products whose URL is known (from config or
  because a listing/feed already showed them). The product page carries the only
  reliable availability signal on nopCommerce shops, so these get `hot_ratio`
  requests for every discovery request.
* **discovery**: the listing/feed sources from config (category page, SmartSearch,
  RSS, Shopify products.json). A paginated listing is swept one page per cycle;
  when a sweep completes, watched products that were previously seen on that
  listing but are missing now (and have no hot page) are marked ABSENT.
"""
from __future__ import annotations

import logging
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from drop_monitor.config import Config, Source
from drop_monitor.fetcher import FetchError, Fetcher, RobotsDisallowed
from drop_monitor.health import write_health
from drop_monitor.matching import Watch, find_watch, matches
from drop_monitor.models import ParseResult, State
from drop_monitor.notifier import Telegram, format_price_change, format_transition
from drop_monitor.parsers import parse
from drop_monitor.state import Transition, mark_absent, observe, touch_checked, watch_key
from drop_monitor.store import Store

log = logging.getLogger(__name__)


@dataclass
class DiscoverySource:
    source: Source
    pending_pages: list[str] = field(default_factory=list)
    sweep_seen: set[str] = field(default_factory=set)  # watch keys matched in the current sweep
    in_sweep: bool = False
    sweeps_completed: int = 0
    disabled_reason: str | None = None
    last_kind: str | None = None

    def next_url(self) -> str:
        if self.pending_pages:
            return self.pending_pages.pop(0)
        self.in_sweep = True
        self.sweep_seen = set()
        return self.source.url


@dataclass
class StepResult:
    kind: str  # "hot" | "discovery" | "idle"
    url: str
    ok: bool
    products: int = 0
    transitions: list[Transition] = field(default_factory=list)
    error: str | None = None
    note: str = ""


class Monitor:
    def __init__(self, config: Config, store: Store, fetcher: Fetcher, telegram: Telegram | None, dry_run: bool = False):
        self.cfg = config
        self.store = store
        self.fetcher = fetcher
        self.tg = telegram
        self.dry_run = dry_run
        self.discovery = [DiscoverySource(s) for s in config.sources]
        self._disc_idx = 0
        self._hot_idx = 0
        self._hot_served = 0
        self.cycle = 0
        self.consecutive_errors = 0
        self.last_error: str | None = None
        self.last_ok_at: str | None = None
        self.backoff_until: float = 0.0
        self._error_notified = False
        self._stop = threading.Event()
        self._next_request_desc = ""

    # ------------------------------------------------------------------ info
    def info(self) -> dict:
        return {
            "cycle": self.cycle,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "backoff_until": self.backoff_until,
            "next_request": self._next_request_desc,
        }

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ scheduling
    def hot_targets(self) -> list[tuple[Watch, str]]:
        if not self.cfg.track_product_pages:
            return []
        out: list[tuple[Watch, str]] = []
        for w in self.cfg.watches:
            url = w.url
            if not url:
                t = self.store.get_tracked(watch_key(w))
                url = t.url if t else None
            if url:
                out.append((w, url))
        return out

    def _active_discovery(self) -> list[DiscoverySource]:
        return [d for d in self.discovery if d.disabled_reason is None]

    def pick(self) -> tuple[str, object, str] | None:
        """Choose the next request: ('hot', watch, url) or ('discovery', dsrc, url)."""
        hot = self.hot_targets()
        disc = self._active_discovery()
        want_hot = bool(hot) and (not disc or self._hot_served < self.cfg.polling.hot_ratio)
        if want_hot:
            self._hot_served += 1
            w, url = hot[self._hot_idx % len(hot)]
            self._hot_idx += 1
            return "hot", w, url
        if disc:
            self._hot_served = 0
            # finish an in-progress sweep before moving to the next source
            d = next((x for x in disc if x.pending_pages), None)
            if d is None:
                d = disc[self._disc_idx % len(disc)]
                self._disc_idx += 1
            return "discovery", d, d.next_url()
        return None

    # ------------------------------------------------------------------ step
    def step(self) -> StepResult:
        """Perform exactly one HTTP request and process it."""
        choice = self.pick()
        self.cycle += 1
        if choice is None:
            return StepResult("idle", "", True, note="nothing to poll (no sources, no known product URLs)")
        kind, target, url = choice
        hint = "product" if kind == "hot" else target.source.type  # type: ignore[union-attr]
        self._next_request_desc = f"{kind} {url}"
        try:
            fetched = self.fetcher.fetch(url)
        except RobotsDisallowed as e:
            msg = str(e)
            log.warning("%s", msg)
            if kind == "discovery":
                target.disabled_reason = msg  # type: ignore[union-attr]
                self._notify(f"⛔️ Sorgente disabilitata (robots.txt): {url}")
            else:
                self.cfg.track_product_pages = False
                self._notify(f"⛔️ robots.txt vieta le pagine prodotto: polling diretto disattivato ({url})")
            return StepResult(kind, url, False, error=msg)
        except FetchError as e:
            return self._handle_fetch_error(kind, target, url, e)

        self._record_ok()
        result = parse(fetched, hint=hint)
        transitions: list[Transition] = []
        if kind == "hot":
            transitions = self._process_hot(target, url, result)  # type: ignore[arg-type]
        else:
            transitions = self._process_discovery(target, url, result)  # type: ignore[arg-type]
        for t in transitions:
            self._dispatch(t)
        log.info("%s %s -> %s products, %d transition(s) (%.2fs)", kind, url, len(result.products), sum(1 for t in transitions if t.changed), fetched.elapsed)
        return StepResult(kind, url, True, products=len(result.products), transitions=transitions)

    def _handle_fetch_error(self, kind: str, target, url: str, e: FetchError) -> StepResult:
        if kind == "hot" and e.is_not_found:
            self._record_ok()  # the site answered; the product is simply gone
            t = mark_absent(self.store, target, reason=f"HTTP {e.status} on product page")
            if t:
                self._dispatch(t)
            return StepResult(kind, url, True, transitions=[t] if t else [], note=f"HTTP {e.status}")
        if kind == "discovery":
            # An incomplete sweep must never be used to declare a product absent.
            target.pending_pages.clear()  # type: ignore[union-attr]
            target.in_sweep = False  # type: ignore[union-attr]
        self.consecutive_errors += 1
        self.last_error = str(e)
        if e.should_backoff:
            base = self.cfg.polling.backoff_base_seconds
            delay = min(base * (2 ** (self.consecutive_errors - 1)), self.cfg.polling.backoff_max_seconds)
            if e.retry_after:
                delay = max(delay, e.retry_after)
            delay *= random.uniform(1.0, 1.25)
            self.backoff_until = time.time() + delay
            log.warning("fetch error (%s); backing off %.0fs (consecutive=%d)", e, delay, self.consecutive_errors)
        else:
            log.warning("fetch error (%s) for %s", e, url)
        n = self.cfg.notify.error_after_consecutive
        if n and self.consecutive_errors >= n and not self._error_notified:
            self._error_notified = True
            self._notify(f"⚠️ drop-monitor: {self.consecutive_errors} errori consecutivi. Ultimo: {e}")
        return StepResult(kind, url, False, error=str(e))

    def _record_ok(self) -> None:
        if self.consecutive_errors and self._error_notified:
            self._notify("✅ drop-monitor: il sito risponde di nuovo")
        self.consecutive_errors = 0
        self._error_notified = False
        self.backoff_until = 0.0
        self.last_error = None
        self.last_ok_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.store.set_meta("last_ok_at", self.last_ok_at)

    # --------------------------------------------------------------- process
    def _process_hot(self, watch: Watch, url: str, result: ParseResult) -> list[Transition]:
        if result.kind != "product" or not result.products:
            # Page answered 200 but is not a product page (e.g. redirect to home).
            t = mark_absent(self.store, watch, reason="product page no longer renders a product")
            return [t] if t else []
        product = result.products[0]
        if not matches(watch, product.title, product.url) and not matches(watch, product.title, url):
            log.warning("product page %s has title %r which does not match %r: trusting the URL", url, product.title, watch.keywords)
        self.store.touch_seen(product.key(), product.title, product.url, product.price, product.availability.value)
        return [observe(self.store, watch, product, "product", url)]

    def _process_discovery(self, d: DiscoverySource, url: str, result: ParseResult) -> list[Transition]:
        d.last_kind = result.kind
        transitions: list[Transition] = []
        for p in result.products:
            self.store.touch_seen(p.key(), p.title, p.url, p.price, p.availability.value)
            w = find_watch(self.cfg.watches, p.title, p.url)
            if w is None:
                continue
            key = watch_key(w)
            if key in d.sweep_seen:
                continue
            d.sweep_seen.add(key)
            transitions.append(observe(self.store, w, p, result.kind, url))
        # pagination: queue the remaining pages of this sweep (one per cycle)
        if d.source.follow_pagination and result.next_pages and url == d.source.url:
            d.pending_pages = [u for u in result.next_pages if u != url]
        if not d.pending_pages:
            transitions.extend(self._complete_sweep(d))
        return transitions

    def _complete_sweep(self, d: DiscoverySource) -> list[Transition]:
        d.in_sweep = False
        d.sweeps_completed += 1
        out: list[Transition] = []
        if d.last_kind in ("rss",):
            return out  # feeds only list new items: silence is not absence
        hot_keys = {watch_key(w) for w, _ in self.hot_targets()}
        for w in self.cfg.watches:
            key = watch_key(w)
            if key in d.sweep_seen or key in hot_keys:
                continue
            tracked = self.store.get_tracked(key)
            if tracked is None:
                touch_checked(self.store, w)
                continue
            if tracked.state == State.ABSENT or not _same_source(tracked.source_url, d.source.url):
                touch_checked(self.store, w)
                continue
            t = mark_absent(self.store, w, reason=f"missing from complete sweep of {d.source.url}")
            if t:
                out.append(t)
        return out

    # ------------------------------------------------------------- notify
    def _dispatch(self, t: Transition) -> None:
        if t.changed:
            log.info("STATE %s: %s -> %s (%s)", t.watch.label, t.old_state.value if t.old_state else None, t.new_state.value, t.reason)
            self._notify(format_transition(t))
        elif t.price_changed and self.cfg.notify.on_price_change:
            self._notify(format_price_change(t))

    def _notify(self, text: str) -> None:
        if self.dry_run or self.tg is None:
            log.info("[notify%s] %s", " dry-run" if self.dry_run else " disabled", text.replace("\n", " | "))
            return
        self.tg.send(text)

    # ----------------------------------------------------------------- loop
    def interval(self) -> float:
        p = self.cfg.polling
        wait = random.uniform(p.min_seconds, p.max_seconds)
        delay = self.fetcher.robots.crawl_delay(self.cfg.sources[0].url) if self.cfg.sources else None
        if delay:
            wait = max(wait, float(delay))
        if self.backoff_until > time.time():
            wait = max(wait, self.backoff_until - time.time())
        return wait

    def write_health(self, status: str = "running") -> None:
        write_health(
            self.cfg.storage.health_path,
            status=status,
            cycle=self.cycle,
            last_ok_at=self.last_ok_at,
            last_error=self.last_error,
            consecutive_errors=self.consecutive_errors,
            backoff_until=self.backoff_until,
        )

    def run_forever(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop())
            except ValueError:  # not main thread
                pass
        log.info("monitor started: %d watch(es), %d source(s), interval %s-%ss, hot_ratio=%d",
                 len(self.cfg.watches), len(self.cfg.sources), self.cfg.polling.min_seconds, self.cfg.polling.max_seconds, self.cfg.polling.hot_ratio)
        if self.cfg.notify.startup_message:
            names = "\n".join(f"• {w.label}" for w in self.cfg.watches)
            self._notify(f"🚀 drop-monitor avviato\nProdotti osservati:\n{names}\nSorgenti: {len(self.cfg.sources)} · intervallo {self.cfg.polling.min_seconds:g}-{self.cfg.polling.max_seconds:g}s")
        self.write_health("starting")
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # never die on an unexpected parser/store error
                log.exception("unexpected error in cycle %d", self.cycle)
                self.consecutive_errors += 1
                self.last_error = "internal error (see log)"
            self.write_health("running")
            wait = self.interval()
            log.debug("sleeping %.1fs", wait)
            # Sleep in slices so the heartbeat stays fresh during a long backoff.
            deadline = time.monotonic() + wait
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._stop.wait(min(remaining, 60))
                self.write_health("backoff" if self.backoff_until > time.time() else "running")
        self.write_health("stopped")
        log.info("monitor stopped")

    def run_once(self, delay: float = 3.0, max_steps: int = 50) -> list[StepResult]:
        """Poll every hot page once and sweep every discovery source once."""
        results: list[StepResult] = []
        hot_needed = len(self.hot_targets())
        saved_ratio = self.cfg.polling.hot_ratio
        for _ in range(max_steps):
            if self._hot_idx >= max(hot_needed, len(self.hot_targets())):
                self.cfg.polling.hot_ratio = 0  # every product page polled once: finish the sweeps only
            r = self.step()
            results.append(r)
            if r.kind == "idle":
                break
            if self.backoff_until > time.time():
                log.warning("in backoff; stopping single run")
                break
            all_swept = all(d.sweeps_completed >= 1 or d.disabled_reason for d in self.discovery)
            hot_done = self._hot_idx >= hot_needed
            if all_swept and hot_done:
                break
            time.sleep(delay)
        self.cfg.polling.hot_ratio = saved_ratio
        self.write_health("once")
        return results


def _same_source(tracked_source_url: str | None, source_url: str) -> bool:
    if not tracked_source_url:
        return False
    a, b = urlsplit(tracked_source_url), urlsplit(source_url)
    return (a.netloc, a.path.rstrip("/")) == (b.netloc, b.path.rstrip("/"))
