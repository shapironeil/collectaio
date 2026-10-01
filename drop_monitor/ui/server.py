"""Local web UI: serves static/index.html and a small JSON API on 127.0.0.1 only.

Routes
  GET  /                          the window
  GET  /api/status                monitor state (SQLite + heartbeat)
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
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from drop_monitor import __version__
from drop_monitor.account import AccountClient, AccountError, build_registration_payload
from drop_monitor.config import Config
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
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
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
        ok, msg = check_health(self.cfg.storage.health_path, self.cfg.storage.health_max_age_seconds)
        return {
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
                if path.startswith("/api/profiles/"):
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
            except (AccountError, ValueError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                log.exception("ui %s failed", path)
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.BAD_GATEWAY)

    return Handler


def serve(cfg: Config, store: Store, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> ThreadingHTTPServer:
    state = UIState(cfg, store)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    url = f"http://{host}:{httpd.server_address[1]}/"
    log.info("drop-monitor window at %s", url)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    return httpd
