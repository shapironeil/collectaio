"""One logged-in browser-like session per buyer profile, with cookies persisted on disk."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
LOGIN_PATH = "/it/login"
ACCOUNT_PATH = "/it/customer/info"


class LoginError(Exception):
    pass


class ShopSession:
    def __init__(self, base_url: str, profile: dict, user_agent: str, sessions_dir: str | Path, timeout: float = 25, probe=None, proxy: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.name = profile.get("name", "default")
        self.sessions_dir = Path(sessions_dir)
        self.probe = probe  # optional callable(label, text) saving pages for study
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            proxy=proxy or None,
        )
        self.proxy = proxy
        self._load_cookies()

    # ---- cookies ---------------------------------------------------------
    @property
    def _cookie_file(self) -> Path:
        return self.sessions_dir / f"{self.name}.json"

    def _load_cookies(self) -> None:
        if self._cookie_file.is_file():
            try:
                for c in json.loads(self._cookie_file.read_text(encoding="utf-8")):
                    self.client.cookies.set(c["name"], c["value"], domain=c.get("domain") or urlsplit(self.base_url).netloc, path=c.get("path", "/"))
            except (ValueError, KeyError):
                log.warning("cookie file for profile %s unreadable: ignored", self.name)

    def save_cookies(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.client.cookies.jar]
        self._cookie_file.write_text(json.dumps(jar), encoding="utf-8")

    def close(self) -> None:
        self.save_cookies()
        self.client.close()

    # ---- http helpers ----------------------------------------------------
    def url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def get(self, path: str, **kw) -> httpx.Response:
        r = self.client.get(self.url(path), **kw)
        self._probe(f"GET {path}", r)
        return r

    def post(self, path: str, data=None, ajax: bool = False, referer: str | None = None, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("Referer", referer or self.url("/it/"))
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        r = self.client.post(self.url(path), data=data, headers=headers, **kw)
        self._probe(f"POST {path}", r)
        return r

    def _probe(self, label: str, r: httpx.Response) -> None:
        if self.probe:
            try:
                self.probe(label, r)
            except Exception:  # pragma: no cover - never break the flow for a probe
                log.exception("probe failed")

    # ---- auth --------------------------------------------------------------
    def is_logged_in(self) -> bool:
        r = self.get(ACCOUNT_PATH)
        return r.status_code == 200 and "/login" not in str(r.url).lower()

    def login(self) -> None:
        email = self.profile.get("account", {}).get("email", "")
        password = self.profile.get("account", {}).get("password", "")
        if not email or not password:
            raise LoginError(f"profilo '{self.name}': e-mail o password mancanti (password in .env)")
        r = self.get(f"{LOGIN_PATH}?returnurl=%2fit%2fcart")
        soup = BeautifulSoup(r.text, "lxml")
        form = soup.find("form", action=re.compile(r"/login", re.I))
        data = {"Email": email, "Password": password, "RememberMe": "true"}
        tok = form.find("input", attrs={"name": "__RequestVerificationToken"}) if form else None
        if tok:
            data["__RequestVerificationToken"] = tok["value"]
        action = form.get("action") if form else f"{LOGIN_PATH}?returnurl=%2fit%2fcart"
        r = self.post(action, data=data, referer=str(r.url))
        if "/login" in str(r.url).lower():
            soup = BeautifulSoup(r.text, "lxml")
            errs = [li.get_text(" ", strip=True) for li in soup.select(".message-error li, .validation-summary-errors li")]
            raise LoginError("login rifiutato: " + ("; ".join(errs) or "credenziali non valide"))
        self.save_cookies()
        log.info("profile %s logged in", self.name)

    def ensure_logged_in(self) -> None:
        if not self.is_logged_in():
            self.login()
