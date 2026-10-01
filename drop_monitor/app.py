"""One process for everything (like an AIO bot): the window + the monitor, controllable from the UI."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from drop_monitor.config import Config, load_config
from drop_monitor.fetcher import Fetcher
from drop_monitor.notifier import TelegramConfirmer
from drop_monitor.notify import build_notifier
from drop_monitor.order.runner import OrderRunner
from drop_monitor.profile import ProfileStore
from drop_monitor.scheduler import Monitor
from drop_monitor.store import Store
from drop_monitor.telegram_bot import CommandBot

log = logging.getLogger(__name__)


def site_base(cfg: Config) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(cfg.sources[0].url) if cfg.sources else urlsplit("https://www.gemcardinfinitycollection.it")
    return f"{u.scheme}://{u.netloc}"


class MonitorController:
    """Starts/stops the monitor thread; reloads config.yaml on every start."""

    def __init__(self, config_path: str, store: Store):
        self.config_path = config_path
        self.store = store
        self.cfg: Config = load_config(config_path)
        self.monitor: Monitor | None = None
        self.thread: threading.Thread | None = None
        self.bot: CommandBot | None = None
        self.notifier = None
        self.fetcher: Fetcher | None = None
        self.dry_run = False
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self.scheduled_at: str | None = None
        self._timer: threading.Timer | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def reload(self) -> Config:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def start(self, dry_run: bool = False) -> dict:
        with self._lock:
            if self.running:
                return self.status()
            try:
                cfg = self.reload()
            except Exception as e:
                self.last_error = f"config non valida: {e}"
                return self.status()
            self.dry_run = dry_run
            self.last_error = None
            base = Path(cfg.path).parent if cfg.path else Path(".")
            self.notifier = None if dry_run else build_notifier(cfg)
            self.fetcher = Fetcher(cfg.polling.user_agent, cfg.polling.timeout_seconds, cfg.polling.accept_language, cfg.polling.respect_robots,
                                   proxies=cfg.network.urls(cfg.network.monitor_group), proxy_mode=cfg.network.proxy_mode)
            confirmer = TelegramConfirmer(self.notifier.telegram) if self.notifier and self.notifier.telegram else None
            runner = None
            if any(t.mode == "auto_checkout" and t.enabled for t in cfg.tasks):
                runner = OrderRunner(site_base(cfg), cfg.polling.user_agent, ProfileStore(base / "personal", base / ".env"), self.store,
                                     confirmer=confirmer, notify=(self.notifier.send if self.notifier else None),
                                     sessions_dir=base / "personal" / "sessions",
                                     proxies=cfg.network.urls() if cfg.network.proxy_mode == "sticky" else None)
            if runner is not None:
                runner.proxy_entries = cfg.network.entries if cfg.network.proxy_mode == "sticky" else []
            self.monitor = Monitor(cfg, self.store, self.fetcher, self.notifier, dry_run=dry_run, order_runner=runner)
            if self.notifier and self.notifier.telegram and cfg.telegram.commands:
                self.bot = CommandBot(self.notifier.telegram, self.store, self.monitor.info, confirmer=confirmer)
                self.bot.start()
            self.thread = threading.Thread(target=self._run, name="monitor", daemon=True)
            self.thread.start()
            log.info("monitor started from the window (dry_run=%s)", dry_run)
            return self.status()

    def _run(self) -> None:
        try:
            self.monitor.run_forever()
        except Exception as e:  # pragma: no cover
            log.exception("monitor thread crashed")
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            if self.bot:
                self.bot.stop()
                self.bot = None
            if self.fetcher:
                self.fetcher.close()

    def stop(self, wait: float = 5.0) -> dict:
        with self._lock:
            if self.monitor:
                self.monitor.stop()
            if self.thread:
                self.thread.join(wait)
            return self.status()

    def restart(self, dry_run: bool | None = None) -> dict:
        self.stop()
        return self.start(self.dry_run if dry_run is None else dry_run)

    def schedule(self, when_iso: str | None, dry_run: bool = False) -> dict:
        """Start the monitor at a given local time (drop time). None cancels."""
        from datetime import datetime

        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.scheduled_at = None
        if when_iso:
            when = datetime.fromisoformat(when_iso)
            delay = (when - datetime.now()).total_seconds()
            if delay <= 0:
                return self.start(dry_run)
            self.scheduled_at = when.isoformat(timespec="minutes")
            self._timer = threading.Timer(delay, lambda: (setattr(self, "scheduled_at", None), self.start(dry_run)))
            self._timer.daemon = True
            self._timer.start()
            log.info("monitor scheduled at %s (in %.0fs)", self.scheduled_at, delay)
        return self.status()

    def status(self) -> dict:
        info = self.monitor.info() if self.monitor else {}
        return {"running": self.running, "dry_run": self.dry_run, "last_error": self.last_error, "scheduled_at": self.scheduled_at, **info}
