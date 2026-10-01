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

    order = sub.add_parser("order", help="run a checkout task now (dry-run unless --live)")
    order.add_argument("--task", required=True, help="task name from config.yaml")
    order.add_argument("--profile", help="only this profile (default: all profiles of the task)")
    order.add_argument("--product-id", help="shop product id; default: the id the monitor has seen for the task's product")
    order.add_argument("--live", action="store_true", help="really place the order (after limits and confirmation)")
    order.add_argument("--probe", action="store_true", help="save every page of the run under data/probes/ for study")
    order.add_argument("--console-confirm", action="store_true", help="confirm on the console instead of Telegram")

    login = sub.add_parser("login", help="test the shop login of a buyer profile")
    login.add_argument("--profile", default="default")

    sub.add_parser("orders", help="print the order history")

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

    if args.cmd == "orders":
        store = Store(cfg.storage.db_path)
        for r in store.recent_orders(30):
            print(f"{r['ts']} {r['task']:15} {r['profile']:10} {r['status']:16} {r['total_eur'] or '':>8} {r['product'] or ''} {r['note'] or ''} {r['order_url'] or ''}")
        return 0

    if args.cmd in ("login", "order"):
        return _run_order_commands(args, cfg)

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
    order_runner = None
    confirmer = None
    if any(t.mode == "auto_checkout" and t.enabled for t in cfg.tasks):
        from pathlib import Path

        from drop_monitor.notifier import TelegramConfirmer
        from drop_monitor.order.runner import OrderRunner
        from drop_monitor.profile import ProfileStore
        from drop_monitor.ui.server import _site_base

        base = Path(cfg.path).parent if cfg.path else Path(".")
        confirmer = TelegramConfirmer(tg) if tg else None
        order_runner = OrderRunner(_site_base(cfg), cfg.polling.user_agent, ProfileStore(base / "personal", base / ".env"), store,
                                   confirmer=confirmer, notify=(tg.send if tg else None), sessions_dir=base / "personal" / "sessions")
    monitor = Monitor(cfg, store, fetcher, tg, dry_run=dry, order_runner=order_runner)

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
        bot = CommandBot(tg, store, monitor.info, confirmer=confirmer)
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


def _run_order_commands(args, cfg) -> int:
    from pathlib import Path

    from drop_monitor.order.runner import Confirmer, OrderRunner, Prober
    from drop_monitor.order.session import LoginError, ShopSession
    from drop_monitor.profile import ProfileStore
    from drop_monitor.state import watch_key
    from drop_monitor.ui.server import _site_base

    base = Path(cfg.path).parent if cfg.path else Path(".")
    profiles = ProfileStore(base / "personal", base / ".env")
    site = _site_base(cfg)
    sessions_dir = base / "personal" / "sessions"

    if args.cmd == "login":
        try:
            prof = profiles.load(args.profile)
            s = ShopSession(site, prof, cfg.polling.user_agent, sessions_dir)
            s.login()
            ok = s.is_logged_in()
            s.close()
            print(f"login profilo '{args.profile}' ({prof['account'].get('email')}): {'OK' if ok else 'FALLITO'}")
            return 0 if ok else 1
        except LoginError as e:
            print(f"login fallito: {e}")
            return 1

    task = next((t for t in cfg.tasks if t.name == args.task), None)
    if task is None:
        print(f"task '{args.task}' non trovato in config.yaml (tasks)", file=sys.stderr)
        return 2
    store = Store(cfg.storage.db_path)
    watch = next((w for w in cfg.watches if task.product in (w.label, w.keywords)), None)
    tracked = store.get_tracked(watch_key(watch)) if watch else None
    product_id = args.product_id or (tracked.product_id if tracked else None)
    title = (tracked.title if tracked and tracked.title else task.product)
    if not product_id:
        print("id prodotto sconosciuto: il monitor non l'ha ancora visto; passa --product-id", file=sys.stderr)
        return 2
    tg = None
    confirmer = Confirmer()
    bot = None
    if cfg.telegram.enabled and not args.console_confirm:
        from drop_monitor.notifier import Telegram, TelegramConfirmer
        from drop_monitor.telegram_bot import CommandBot

        tg = Telegram(cfg.telegram.bot_token, cfg.telegram.chat_id)
        confirmer = TelegramConfirmer(tg)
        bot = CommandBot(tg, store, lambda: {}, confirmer=confirmer)
        bot.start()
    runner = OrderRunner(site, cfg.polling.user_agent, profiles, store, confirmer=confirmer,
                         notify=(tg.send if tg else lambda t: print(t)), sessions_dir=sessions_dir,
                         probe_dir=str(base / "data" / "probes") if args.probe else None)
    mode = "LIVE" if args.live else "DRY-RUN (nessun ordine inviato)"
    print(f"task {task.name} · prodotto {title} (id {product_id}) · profili {task.profiles} · {mode}")
    results = runner.run_task(task, product_id, title, dry_run=not args.live, only_profile=args.profile)
    for r in results:
        print(f"\n== profilo {r.profile}: {r.status} {('%.2f €' % r.total_eur) if r.total_eur is not None else ''} {r.message}")
        for st in r.steps:
            print(f"   {'OK ' if st.ok else 'KO '} {st.step}: {st.detail}")
        if r.order_url:
            print(f"   link: {r.order_url}")
    if bot:
        bot.stop()
    return 0 if results and all(r.status in ("placed", "pending_payment", "dry_run") for r in results) else 1
