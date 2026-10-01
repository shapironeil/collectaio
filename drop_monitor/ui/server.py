"""Local web UI: serves static/index.html and a small JSON API on 127.0.0.1 only.

Routes
  GET  /                          the window
  GET  /api/status                monitor state (SQLite + heartbeat + in-process controller)
  GET  /api/config                editable config (secrets masked)      PUT /api/config  save + validate
  POST /api/monitor/start|stop|restart   {"dry_run": false}  (only in `drop-monitor app` mode)
  GET  /api/profiles              list of buyer profiles (passwords masked)
  PUT  /api/profiles/<name>       save a profile; body may carry "password" (stored in .env, never in YAML)
  DELETE /api/profiles/<name>     remove a profile
  POST /api/register/start        fetch the shop's registration form (fields, countries, captcha)
  GET  /api/register/captcha      the captcha image (proxied from the shop, same session)
  POST /api/register/refresh      new captcha image
  GET  /api/states?country=46     provinces for a country
  POST /api/register/preview      payload the site would receive (password masked), no submission
  POST /api/register/submit       {"profile": "default", "captcha_answer": "...", "password": "...", "newsletter": false}
"""
from __future__ import annotations

import json
import logging
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from drop_monitor import __version__
from drop_monitor.account import AccountClient, AccountError, build_registration_payload
from drop_monitor.config import Config, ConfigError
from drop_monitor.config_edit import public_config, read_raw, save_config
from drop_monitor.sites import all_modules, module_for
from drop_monitor.health import check_health
from drop_monitor.profile import ProfileStore
from drop_monitor.store import Store

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    b'<stop offset="0" stop-color="#7C8CFF"/><stop offset="1" stop-color="#B388FF"/></linearGradient></defs>'
    b'<rect width="64" height="64" rx="18" fill="url(#g)"/><text x="32" y="43" font-size="34" font-family="Arial" font-weight="700" '
    b'fill="#fff" text-anchor="middle">C</text></svg>'
)


class UIState:
    def __init__(self, cfg: Config, store: Store, controller=None):
        self.cfg = cfg
        self.store = store
        self.controller = controller  # MonitorController in app mode
        base = Path(cfg.path).parent if cfg.path else Path(".")
        self.profiles = ProfileStore(base / "personal", base / ".env")
        self.site = _site_base(cfg)
        self.account: AccountClient | None = None
        self.lock = threading.Lock()

    def client(self) -> AccountClient:
        if self.account is None:
            self.account = AccountClient(self.site, self.cfg.polling.user_agent, self.cfg.polling.timeout_seconds)
        return self.account

    # ---- handlers -------------------------------------------------------
    def status(self) -> dict:
        if self.controller is not None:
            self.cfg = self.controller.cfg
        ok, msg = check_health(self.cfg.storage.health_path, self.cfg.storage.health_max_age_seconds)
        return {
            "controller": self.controller.status() if self.controller else None,
            "version": __version__,
            "site": self.site,
            "health": {"ok": ok, "message": msg},
            "watches": [{"label": w.label, "keywords": w.keywords, "exclude": w.exclude, "url": w.url} for w in self.cfg.watches],
            "sources": [{"type": s.type, "url": s.url} for s in self.cfg.sources],
            "polling": {"min": self.cfg.polling.min_seconds, "max": self.cfg.polling.max_seconds, "hot_ratio": self.cfg.polling.hot_ratio},
            "tracked": [
                {"label": t.label, "state": t.state.value, "title": t.title, "url": t.url, "price": t.price,
                 "last_seen": t.last_seen, "last_checked": t.last_checked, "source": t.source_kind}
                for t in self.store.all_tracked()
            ],
            "events": self.store.recent_events(15),
            "tasks": [{"name": t.name, "product": t.product, "profiles": t.profiles, "quantity": t.quantity, "mode": t.mode,
                       "max_total_eur": t.max_total_eur, "confirm_on_telegram": t.confirm_on_telegram, "enabled": t.enabled} for t in self.cfg.tasks],
            "orders": self.store.recent_orders(15),
            "seen": self.store.seen_count(),
            "telegram": {"enabled": self.cfg.telegram.enabled, "chat_id": self.cfg.telegram.chat_id[-4:].rjust(len(self.cfg.telegram.chat_id), "*") if self.cfg.telegram.chat_id else ""},
        }

    def get_config(self) -> dict:
        return public_config(self.cfg.path)

    def put_config(self, body: dict) -> dict:
        out = save_config(self.cfg.path, body, self.profiles.env_file)
        if self.controller is not None:
            self.controller.reload()
            self.cfg = self.controller.cfg
            out["monitor_running"] = self.controller.running
        return out

    # ---- proxies -----------------------------------------------------------
    def _proxy_store(self):
        from drop_monitor.proxies import ResultStore

        base = Path(self.cfg.path).parent if self.cfg.path else Path(".")
        return ResultStore(base / "data" / "proxy-tests.json")

    def proxies(self) -> dict:
        from drop_monitor.proxies import entries_from_config, mask_proxy

        raw = read_raw(self.cfg.path)
        net = raw.get("network") or {}
        entries = entries_from_config(net.get("proxies"))
        results = self._proxy_store()
        return {
            "proxy_mode": net.get("proxy_mode", "off"), "monitor_group": net.get("monitor_group", ""),
            "groups": sorted({e.group for e in entries} | {"default"}),
            "entries": [{"url": e.url, "masked": mask_proxy(e.url), "group": e.group, "enabled": e.enabled, "label": e.label, "test": results.get(e.url)} for e in entries],
        }

    def put_proxies(self, body: dict) -> dict:
        entries = []
        for e in body.get("entries") or []:
            if isinstance(e, dict) and str(e.get("url", "")).strip():
                entries.append({"url": str(e["url"]).strip(), "group": str(e.get("group") or "default"), "enabled": bool(e.get("enabled", True)), "label": str(e.get("label") or "")})
        net = {"proxies": entries}
        if "proxy_mode" in body:
            net["proxy_mode"] = body["proxy_mode"]
        if "monitor_group" in body:
            net["monitor_group"] = body["monitor_group"] or ""
        save_config(self.cfg.path, {"network": net}, self.profiles.env_file)
        if self.controller is not None:
            self.controller.reload()
            self.cfg = self.controller.cfg
        return self.proxies()

    def import_proxies(self, body: dict) -> dict:
        from drop_monitor.proxies import mask_proxy, parse_proxy_list

        urls, errors = parse_proxy_list(body.get("text") or "", body.get("scheme") or "http")
        return {"urls": urls, "masked": [mask_proxy(u) for u in urls], "errors": errors}

    def test_proxies(self, body: dict) -> dict:
        from drop_monitor.proxies import entries_from_config, test_many

        urls = body.get("urls")
        if body.get("all") or not urls:
            urls = [e.url for e in entries_from_config((read_raw(self.cfg.path).get("network") or {}).get("proxies"))]
        target = body.get("target") or (self.site + "/")
        results = test_many(list(urls), target, timeout=float(body.get("timeout") or 12), ip_service=None if body.get("no_ip") else "https://api.ipify.org")
        self._proxy_store().update(results)
        return {"results": [r.__dict__ for r in results]}

    def modules(self) -> dict:
        cur = module_for(self.site)
        return {"modules": [m.to_dict() for m in all_modules()], "current": cur.key if cur else None}

    def quick_task(self, body: dict) -> dict:
        """Paste a product URL: fetch it, read the title, add product + task to config.yaml."""
        from drop_monitor.fetcher import Fetcher
        from drop_monitor.parsers import parse

        url = (body.get("url") or "").strip()
        if not url.startswith("http"):
            raise AccountError("incolla l'URL completo della pagina prodotto")
        f = Fetcher(self.cfg.polling.user_agent, self.cfg.polling.timeout_seconds, respect_robots=self.cfg.polling.respect_robots)
        try:
            res = parse(f.fetch(url), hint="product")
        finally:
            f.close()
        if not res.products:
            raise AccountError("nessun prodotto riconosciuto a quell'URL")
        prod = res.products[0]
        raw = read_raw(self.cfg.path)
        products = list(raw.get("products") or [])
        name = body.get("name") or prod.title
        if not any((p.get("url") if isinstance(p, dict) else "") == prod.url for p in products):
            products.append({"keywords": prod.title, "name": name, "url": prod.url})
        tasks = list(raw.get("tasks") or [])
        task = {"name": body.get("task_name") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30], "product": name,
                "profiles": body.get("profiles") or ["default"], "quantity": int(body.get("quantity") or 1),
                "mode": body.get("mode") or "monitor", "max_total_eur": float(body.get("max_total_eur") or 150)}
        tasks = [t for t in tasks if t.get("name") != task["name"]] + [task]
        out = save_config(self.cfg.path, {"products": products, "tasks": tasks}, self.profiles.env_file)
        if self.controller is not None:
            self.controller.reload()
            self.cfg = self.controller.cfg
        return {"product": {"title": prod.title, "url": prod.url, "price": prod.price, "availability": prod.availability.value, "id": prod.product_id}, "task": task, "config": out}

    def update_check(self) -> dict:
        from drop_monitor.updater import Updater

        u = Updater()
        try:
            return u.check()
        finally:
            u.close()

    def update_apply(self) -> dict:
        from drop_monitor.updater import Updater

        if self.controller is not None and self.controller.running:
            self.controller.stop()
        u = Updater()
        try:
            return u.apply()
        finally:
            u.close()

    def restart(self) -> dict:
        from drop_monitor.updater import restart_app

        if self.controller is not None and self.controller.running:
            self.controller.stop()
        threading.Timer(0.8, restart_app).start()
        return {"ok": True, "message": "riavvio in corso"}

    def monitor_action(self, action: str, body: dict) -> dict:
        if self.controller is None:
            raise AccountError("monitor non controllabile da qui: avvia con `drop-monitor app` (windows\\app.bat)")
        dry = bool(body.get("dry_run", False))
        if action == "schedule":
            return self.controller.schedule(body.get("at") or None, dry)
        if action == "start":
            return self.controller.start(dry)
        if action == "stop":
            return self.controller.stop()
        if action == "restart":
            return self.controller.restart(dry if "dry_run" in body else None)
        raise AccountError("azione sconosciuta")

    def list_profiles(self) -> dict:
        return {"profiles": [self.profiles.public(n) for n in self.profiles.names()]}

    def put_profile(self, name: str, body: dict) -> dict:
        password = body.pop("password", "") or (body.get("account") or {}).pop("password", "")
        self.profiles.save(name, body, password=password or None)
        return self.profiles.public(name)

    def delete_profile(self, name: str) -> dict:
        self.profiles.delete(name)
        return self.list_profiles()

    def register_start(self) -> dict:
        with self.lock:
            form = self.client().start_registration("/it/cart")
        return form.to_dict()

    def register_captcha(self) -> tuple[bytes, str]:
        with self.lock:
            return self.client().captcha_image()

    def register_refresh(self) -> dict:
        with self.lock:
            self.client().refresh_captcha()
        return {"ok": True}

    def states(self, country: str) -> list[dict]:
        with self.lock:
            return self.client().states(country)

    def register_payload(self, body: dict) -> dict[str, str]:
        client = self.client()
        if client.form is None:
            raise AccountError("Prima carica il modulo di registrazione.")
        profile = self.profiles.load(body.get("profile") or "default")
        password = body.get("password") or profile["account"].get("password") or ""
        return build_registration_payload(client.form, profile, password, body.get("captcha_answer"), body.get("newsletter"))

    def register_preview(self, body: dict) -> dict:
        try:
            payload = self.register_payload(body)
            masked = {k: ("••••••" if "assword" in k else v) for k, v in payload.items()}
            masked["__RequestVerificationToken"] = masked["__RequestVerificationToken"][:12] + "…"
            return {"ok": True, "payload": masked, "action": self.client().form.action}
        except AccountError as e:
            return {"ok": False, "error": str(e)}

    def register_submit(self, body: dict) -> dict:
        try:
            payload = self.register_payload(body)
        except AccountError as e:
            return {"ok": False, "message": str(e), "errors": []}
        with self.lock:
            res = self.client().submit_registration(payload)
        if res.ok and body.get("save_password") and body.get("password"):
            self.profiles.save(body.get("profile") or "default", self.profiles.load(body.get("profile") or "default"), password=body["password"])
        return {"ok": res.ok, "message": res.message, "errors": res.errors, "final_url": res.final_url, "result_id": res.result_id}


def _site_base(cfg: Config) -> str:
    u = urlsplit(cfg.sources[0].url) if cfg.sources else urlsplit("https://www.gemcardinfinitycollection.it")
    return f"{u.scheme}://{u.netloc}"


def make_handler(state: UIState):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"drop-monitor-ui/{__version__}"

        def log_message(self, fmt, *args):  # route to logging instead of stderr
            log.debug("ui %s", fmt % args)

        # ---- helpers ------------------------------------------------------
        def _json(self, data, status=HTTPStatus.OK):
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _bytes(self, data: bytes, ctype: str):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            # Only same-origin browser requests: block other sites embedding/calling the API.
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            host = self.headers.get("Host", "")
            if origin and host and host not in origin:
                self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
                return False
            return True

        # ---- routes -------------------------------------------------------
        def do_GET(self):
            path = urlsplit(self.path).path
            q = parse_qs(urlsplit(self.path).query)
            try:
                if path in ("/", "/index.html"):
                    self._bytes((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/favicon.ico":
                    self._bytes(FAVICON, "image/svg+xml")
                elif path == "/api/status":
                    self._json(state.status())
                elif path == "/api/profiles":
                    self._json(state.list_profiles())
                elif path == "/api/config":
                    self._json(state.get_config())
                elif path == "/api/modules":
                    self._json(state.modules())
                elif path == "/api/proxies":
                    self._json(state.proxies())
                elif path == "/api/update/check":
                    self._json(state.update_check())
                elif path == "/api/register/captcha":
                    data, ctype = state.register_captcha()
                    self._bytes(data, ctype)
                elif path == "/api/states":
                    self._json(state.states(q.get("country", ["46"])[0]))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except AccountError as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # network errors to the shop etc.
                log.exception("ui GET %s failed", path)
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.BAD_GATEWAY)

        def do_POST(self):
            self._mutate()

        def do_PUT(self):
            self._mutate()

        def do_DELETE(self):
            self._mutate()

        def _mutate(self):
            if not self._guard():
                return
            path = urlsplit(self.path).path
            body = self._body()
            try:
                if path == "/api/config":
                    self._json(state.put_config(body))
                elif path == "/api/quick-task":
                    self._json(state.quick_task(body))
                elif path == "/api/proxies":
                    self._json(state.put_proxies(body))
                elif path == "/api/proxies/import":
                    self._json(state.import_proxies(body))
                elif path == "/api/proxies/test":
                    self._json(state.test_proxies(body))
                elif path == "/api/update/apply":
                    self._json(state.update_apply())
                elif path == "/api/restart":
                    self._json(state.restart())
                elif path.startswith("/api/monitor/"):
                    self._json(state.monitor_action(path.rsplit("/", 1)[1], body))
                elif path.startswith("/api/profiles/"):
                    name = unquote(path.rsplit("/", 1)[1])
                    self._json(state.delete_profile(name) if self.command == "DELETE" else state.put_profile(name, body))
                elif path == "/api/register/start":
                    self._json(state.register_start())
                elif path == "/api/register/refresh":
                    self._json(state.register_refresh())
                elif path == "/api/register/preview":
                    self._json(state.register_preview(body))
                elif path == "/api/register/submit":
                    self._json(state.register_submit(body))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (AccountError, ValueError, ConfigError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                log.exception("ui %s failed", path)
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.BAD_GATEWAY)

    return Handler


def serve(cfg: Config, store: Store, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, controller=None) -> ThreadingHTTPServer:
    state = UIState(cfg, store, controller=controller)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    url = f"http://{host}:{httpd.server_address[1]}/"
    log.info("drop-monitor window at %s", url)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    return httpd
