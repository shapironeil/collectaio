"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import re
import sys

from drop_monitor import __version__
from drop_monitor.config import ConfigError, load_config
from drop_monitor.fetcher import FetchError, Fetcher, RobotsDisallowed
from drop_monitor.health import check_health
from drop_monitor.logging_setup import setup_logging
from drop_monitor.matching import find_watch
from drop_monitor.models import FetchResult
from drop_monitor.notifier import Telegram
from drop_monitor.parsers import parse
from drop_monitor.scheduler import Monitor
from drop_monitor.store import Store
from drop_monitor.telegram_bot import CommandBot, build_status

log = logging.getLogger("drop_monitor")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="drop-monitor", description="Watch an e-commerce site for product drops and restocks.")
    ap.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    ap.add_argument("--version", action="version", version=f"drop-monitor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the monitor loop forever")
    run.add_argument("--dry-run", action="store_true", help="log notifications instead of sending them")

    once = sub.add_parser("once", help="one full sweep of every source + every product page, then exit")
    once.add_argument("--dry-run", action="store_true", help="log notifications instead of sending them (default: send)")
    once.add_argument("--delay", type=float, default=3.0, help="seconds between requests during the sweep (default 3)")

    probe = sub.add_parser("probe", help="fetch one URL, parse it and show what the monitor would see")
    probe.add_argument("url")
    probe.add_argument("--type", default="auto", choices=["auto", "category", "search", "product", "rss", "shopify"])
    probe.add_argument("--file", help="parse a saved HTML/XML file instead of fetching (url is still used as base)")

    ui = sub.add_parser("ui", help="open the local control window (glass UI) in the browser")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--no-browser", action="store_true")

    sub.add_parser("status", help="print the tracked state from the database")
    sub.add_parser("healthcheck", help="exit 0 if the monitor heartbeat is fresh")
    sub.add_parser("test-telegram", help="send a test message to the configured chat")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.storage.log_path if args.cmd in ("run", "once", "ui") else None, cfg.storage.log_level)

    if args.cmd == "healthcheck":
        ok, msg = check_health(cfg.storage.health_path, cfg.storage.health_max_age_seconds)
        print(msg)
        return 0 if ok else 1

    if args.cmd == "ui":
        from drop_monitor.ui.server import serve

        store = Store(cfg.storage.db_path)
        httpd = serve(cfg, store, host=args.host, port=args.port, open_browser=not args.no_browser)
        print(f"drop-monitor window: http://{args.host}:{httpd.server_address[1]}/  (Ctrl+C per chiudere)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            store.close()
        return 0

    if args.cmd == "status":
        store = Store(cfg.storage.db_path)
        print(_strip_html(build_status(store)))
        for ev in store.recent_events(10):
            print(f"  {ev['ts']} {ev['watch_key']}: {ev['old_state']} -> {ev['new_state']} {ev['price'] or ''} ({ev['note']})")
        return 0

    if args.cmd == "test-telegram":
        if not cfg.telegram.enabled:
            print("telegram.enabled is false")
            return 1
        tg = Telegram(cfg.telegram.bot_token, cfg.telegram.chat_id)
        me = tg.get_me()
        ok = tg.send(f"✅ drop-monitor test: bot @{me.get('username')} collegato a questa chat.")
        print("sent" if ok else "FAILED (see log)")
        return 0 if ok else 1

    fetcher = Fetcher(cfg.polling.user_agent, cfg.polling.timeout_seconds, cfg.polling.accept_language, cfg.polling.respect_robots)

    if args.cmd == "probe":
        if args.file:
            text = open(args.file, encoding="utf-8").read()
            fetched = FetchResult(url=args.url, status=200, text=text, content_type="application/rss+xml" if args.file.endswith(".xml") else "text/html")
        else:
            try:
                fetched = fetcher.fetch(args.url)
            except (FetchError, RobotsDisallowed) as e:
                print(f"fetch failed: {e}", file=sys.stderr)
                return 1
        result = parse(fetched, hint=args.type)
        print(f"kind={result.kind} products={len(result.products)} page={result.page_number}/{result.total_pages} next_pages={len(result.next_pages)}")
        for p in result.products:
            w = find_watch(cfg.watches, p.title, p.url)
            flag = f"  <== {w.label}" if w else ""
            print(f"[{p.availability.value:11}] {p.price or '-':>10}  {p.title}{flag}\n              {p.url}")
        return 0

    store = Store(cfg.storage.db_path)
    dry = bool(getattr(args, "dry_run", False))
    tg = Telegram(cfg.telegram.bot_token, cfg.telegram.chat_id) if cfg.telegram.enabled and not dry else None
    monitor = Monitor(cfg, store, fetcher, tg, dry_run=dry)

    if args.cmd == "once":
        results = monitor.run_once(delay=args.delay)
        for r in results:
            status = "ok" if r.ok else f"ERR {r.error}"
            changes = ", ".join(f"{t.watch.label}: {t.old_state.value if t.old_state else None}->{t.new_state.value}" for t in r.transitions if t.changed)
            print(f"{r.kind:9} {status:30} {r.products:3} products  {r.url} {changes}")
        print()
        print(_strip_html(build_status(store, monitor.info())))
        return 0

    bot = None
    if tg is not None and cfg.telegram.commands:
        bot = CommandBot(tg, store, monitor.info)
        bot.start()
    try:
        monitor.run_forever()
    finally:
        if bot:
            bot.stop()
        fetcher.close()
        if tg:
            tg.close()
        store.close()
    return 0
