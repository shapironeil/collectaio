"""Registration in a real (visible) browser with Playwright, step by step, with live status.

The person watches Chromium open the shop, click "Registrati", fill every field from the buyer
profile, pick country/province, tick the consents, then WAIT for the captcha answer typed by the
person in the page (never solved by software). In dry-run it stops there; otherwise it clicks
"Registrati", reads the outcome, saves the password to .env and the cookies for later logins.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from drop_monitor.account import REGISTER_RESULT_MESSAGES

log = logging.getLogger(__name__)

FIELD_MAP = {  # site input name -> (profile section, key)
    "FirstName": ("shipping", "first_name"), "LastName": ("shipping", "last_name"), "Email": ("account", "email"),
    "Company": ("shipping", "company"), "StreetAddress": ("shipping", "address1"), "ZipPostalCode": ("shipping", "zip"),
    "City": ("shipping", "city"), "Phone": ("shipping", "phone"), "customer_attribute_1": ("billing", "fiscal_code"),
    "customer_attribute_2": ("billing", "vat_number"), "customer_attribute_6": ("billing", "sdi_code"), "customer_attribute_7": ("billing", "pec"),
}


def playwright_status() -> dict:
    """Is Playwright importable and is Chromium downloaded?"""
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"installed": False, "browser": False, "detail": "pacchetto playwright non installato"}
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            ok = bool(path) and Path(path).exists()
            return {"installed": True, "browser": ok, "detail": path if ok else "Chromium non scaricato"}
    except Exception as e:  # pragma: no cover
        return {"installed": True, "browser": False, "detail": str(e)}


def install_browser(log_cb=None) -> dict:
    """pip install playwright + download Chromium (used by the window's button)."""
    import subprocess
    import sys

    out = []
    for cmd in ([sys.executable, "-m", "pip", "install", "--quiet", "--no-warn-script-location", "playwright>=1.45"],
                [sys.executable, "-m", "playwright", "install", "chromium"]):
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        out.append((res.stdout + res.stderr)[-1500:])
        if log_cb:
            log_cb(" ".join(cmd[2:]) + "\n" + out[-1])
        if res.returncode != 0:
            return {"ok": False, "log": "\n".join(out)}
    return {"ok": True, "log": "\n".join(out)}


@dataclass
class RunState:
    profile: str
    dry_run: bool
    status: str = "idle"  # running | waiting_captcha | done | failed | cancelled
    steps: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    result: dict | None = None
    started_at: float = field(default_factory=time.time)
    cancel: bool = False
    captcha_answer: str | None = None  # can also be typed in the window instead of the browser

    def log(self, step: str, status: str = "ok", detail: str = "") -> None:
        self.steps.append({"ts": datetime.now().strftime("%H:%M:%S"), "step": step, "status": status, "detail": detail})
        log.info("browser-reg [%s] %s: %s %s", self.profile, step, status, detail)

    def to_dict(self) -> dict:
        return {"profile": self.profile, "dry_run": self.dry_run, "status": self.status, "steps": self.steps,
                "screenshots": len(self.screenshots), "result": self.result, "elapsed": int(time.time() - self.started_at)}


class BrowserRegistrar:
    def __init__(self, base_url: str, profiles, sessions_dir: str | Path, shots_dir: str | Path, headless: bool = False, slow_mo: int = 150):
        self.base_url = base_url.rstrip("/")
        self.profiles = profiles
        self.sessions_dir = Path(sessions_dir)
        self.shots_dir = Path(shots_dir)
        self.headless = headless
        self.slow_mo = slow_mo
        self.state: RunState | None = None
        self._thread: threading.Thread | None = None

    # ---- control ----------------------------------------------------------
    def start(self, profile_name: str, password: str | None, dry_run: bool = True, captcha_timeout: int = 180, register_url: str | None = None) -> RunState:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("una registrazione nel browser è già in corso")
        self.state = RunState(profile=profile_name, dry_run=dry_run)
        self._thread = threading.Thread(target=self._run, args=(profile_name, password, dry_run, captcha_timeout, register_url), name="browser-registration", daemon=True)
        self._thread.start()
        return self.state

    def cancel(self) -> None:
        if self.state:
            self.state.cancel = True

    def answer_captcha(self, answer: str) -> None:
        if self.state:
            self.state.captcha_answer = answer

    # ---- the run ------------------------------------------------------------
    def _run(self, profile_name: str, password: str | None, dry_run: bool, captcha_timeout: int, register_url: str | None) -> None:
        st = self.state
        st.status = "running"
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            st.status = "failed"
            st.log("playwright", "error", "pacchetto playwright non installato: usa 'Installa browser' in Impostazioni")
            return
        profile = self.profiles.load(profile_name)
        password = password or profile["account"].get("password") or ""
        run_dir = self.shots_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        st.log("profilo", "ok", f"{profile_name} · {profile['account'].get('email') or 'senza e-mail'}")
        if len(password) < 6:
            st.status = "failed"
            st.log("password", "error", "serve una password di almeno 6 caratteri (profilo o campo nella finestra)")
            return
        try:
            with sync_playwright() as p:
                import os

                exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None
                browser = p.chromium.launch(headless=self.headless, slow_mo=self.slow_mo, executable_path=exe, args=["--no-sandbox"] if exe else None)
                ctx = browser.new_context(locale="it-IT", viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                shot = lambda name: self._shot(page, run_dir, name)
                try:
                    self._flow(page, ctx, profile, password, dry_run, captcha_timeout, shot, register_url)
                finally:
                    if not self.headless and st.status in ("done", "failed"):
                        time.sleep(4)  # leave the result visible
                    ctx.close()
                    browser.close()
        except Exception as e:
            st.status = "failed"
            st.log("browser", "error", f"{type(e).__name__}: {e}")
            log.exception("browser registration crashed")

    def _flow(self, page, ctx, profile, password, dry_run, captcha_timeout, shot, register_url):
        st = self.state
        home = self.base_url + "/it/"
        page.goto(home, wait_until="domcontentloaded")
        st.log("apertura sito", "ok", home)
        shot("01-home")
        # Click the registration link like a person would; fall back to the direct URL.
        link = page.locator('a[href*="/register"]').first
        if link.count():
            link.click()
            page.wait_for_load_state("domcontentloaded")
            st.log("click Registrati", "ok", page.url)
        else:
            page.goto(register_url or f"{self.base_url}/it/register", wait_until="domcontentloaded")
            st.log("apertura modulo", "ok", page.url)
        page.wait_for_selector("form[action*='register'] input[name='Email']", timeout=20000)
        shot("02-modulo")
        if st.cancel:
            return self._cancelled()
        # Text fields
        for name, (sec, key) in FIELD_MAP.items():
            val = str(profile.get(sec, {}).get(key, "") or "")
            if name == "StreetAddress":
                val = " ".join(x for x in (profile["shipping"].get("address1", ""), profile["shipping"].get("address2", "")) if x).strip()
            loc = page.locator(f"input[name='{name}']")
            if loc.count() and val:
                loc.first.click()
                loc.first.fill("")
                loc.first.type(val, delay=25)
                st.log(f"campo {name}", "ok", val if name != "Email" else val)
        # Country then province (AJAX)
        country_id = str(profile["shipping"].get("country_id") or 46)
        if page.locator("select[name='CountryId']").count():
            page.select_option("select[name='CountryId']", country_id)
            st.log("nazione", "ok", country_id)
            prov = str(profile["shipping"].get("province_id") or 0)
            prov_name = profile["shipping"].get("province") or ""
            try:
                try:
                    page.wait_for_function("() => document.querySelectorAll(\"select[name='StateProvinceId'] option\").length > 1", timeout=8000)
                except Exception:
                    # The site's JS did not populate the list: fetch the provinces ourselves (same endpoint the page uses).
                    page.evaluate("""async (cid) => {
                        const r = await fetch('/country/getstatesbycountryid?countryId=' + cid + '&addSelectStateItem=true');
                        const data = await r.json(); const sel = document.querySelector("select[name='StateProvinceId']");
                        sel.innerHTML = data.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
                    }""", country_id)
                    st.log("province", "ok", "caricate dall'endpoint del sito")
                if prov and prov != "0":
                    page.select_option("select[name='StateProvinceId']", prov)
                elif prov_name:
                    page.select_option("select[name='StateProvinceId']", label=prov_name)
                st.log("provincia", "ok", prov_name or prov)
            except Exception as e:
                st.log("provincia", "warn", f"non selezionata: {e}")
        # Newsletter + referral + consents
        news = bool(profile.get("preferences", {}).get("newsletter"))
        if page.locator("input[name='Newsletter'][type='checkbox']").count():
            page.locator("input[name='Newsletter'][type='checkbox']").set_checked(news)
            st.log("newsletter", "ok", "sì" if news else "no")
        ref = str(profile.get("preferences", {}).get("referral") or "")
        if ref and page.locator(f"input[name='customer_attribute_3'][value='{ref}']").count():
            page.locator(f"input[name='customer_attribute_3'][value='{ref}']").check()
            st.log("come ci hai conosciuto", "ok", ref)
        for cb in page.locator("input[type='checkbox'][name^='accept-']").all():
            cb.check()
        st.log("consensi", "ok", "privacy, condizioni, informativa")
        # Password
        page.fill("input[name='Password']", password)
        page.fill("input[name='ConfirmPassword']", password)
        st.log("password", "ok", "•" * len(password))
        shot("03-compilato")
        # Captcha: typed by the person (in the browser or in the window)
        cap = page.locator("input[name='CaptchaInputText']")
        if cap.count():
            st.status = "waiting_captcha"
            st.log("captcha", "wait", "scrivi la risposta nel browser (o nella finestra)")
            cap.first.focus()
            deadline = time.time() + captcha_timeout
            answer = ""
            while time.time() < deadline and not st.cancel:
                if st.captcha_answer:
                    cap.first.fill(st.captcha_answer)
                    answer = st.captcha_answer
                    break
                answer = cap.first.input_value().strip()
                if answer and len(answer) >= 1 and page.evaluate("() => document.activeElement !== document.querySelector(\"input[name='CaptchaInputText']\")"):
                    break  # the person left the field after typing
                time.sleep(0.5)
            if st.cancel:
                return self._cancelled()
            if not answer:
                st.status = "failed"
                st.log("captcha", "error", "nessuna risposta entro il tempo limite")
                return
            st.status = "running"
            st.log("captcha", "ok", answer)
        shot("04-pronto")
        if dry_run:
            st.status = "done"
            st.result = {"ok": True, "dry_run": True, "message": "Dry-run: modulo compilato, 'Registrati' NON premuto."}
            st.log("dry-run", "ok", "fermato prima dell'invio")
            return
        page.click("#register-button, input[name='register-button']")
        st.log("click Registrati", "ok", "")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        shot("05-esito")
        url = page.url
        html = page.content()
        res = self._classify(url, html)
        st.result = res
        if res["ok"]:
            st.status = "done"
            st.log("esito", "ok", res["message"])
            self.profiles.save(profile["name"], {k: v for k, v in profile.items() if k in ("account", "shipping", "billing", "payment", "preferences")} | {
                "account": {**profile["account"], "registered_at": datetime.now().isoformat(timespec="seconds"), "registered_site": self.base_url, "registration_result": res.get("result_id")}}, password=password)
            self._save_cookies(ctx, profile["name"])
            st.log("account salvato", "ok", f"password in .env, cookie in personal/sessions/{profile['name']}.json")
        else:
            st.status = "failed"
            st.log("esito", "error", res["message"] + (" — " + "; ".join(res.get("errors", [])) if res.get("errors") else ""))

    def _classify(self, url: str, html: str) -> dict:
        from drop_monitor.account import classify_registration_response

        r = classify_registration_response(html, url, "/it/cart")
        return {"ok": r.ok, "message": r.message, "errors": r.errors, "final_url": r.final_url, "result_id": r.result_id,
                "explain": REGISTER_RESULT_MESSAGES.get(r.result_id or "", "")}

    def _save_cookies(self, ctx, name: str) -> None:
        import json

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        jar = [{"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")} for c in ctx.cookies()]
        (self.sessions_dir / f"{name}.json").write_text(json.dumps(jar), encoding="utf-8")

    def _shot(self, page, run_dir: Path, name: str) -> None:
        try:
            path = run_dir / f"{name}.png"
            page.screenshot(path=str(path), full_page=False)
            self.state.screenshots.append(str(path))
        except Exception as e:  # pragma: no cover
            log.warning("screenshot failed: %s", e)

    def _cancelled(self) -> None:
        self.state.status = "cancelled"
        self.state.log("annullato", "warn", "dall'utente")


DEMO_PROFILE = {
    "account": {"email": "mario.rossi.demo@example.com"},
    "shipping": {"first_name": "Mario", "last_name": "Rossi", "company": "", "address1": "Via Roma 1", "address2": "Interno 3", "zip": "20121",
                 "city": "Milano", "province": "Milano", "province_id": 130, "country": "Italy", "country_id": 46, "phone": "3331234567"},
    "billing": {"same_as_shipping": True, "fiscal_code": "RSSMRA80A01F205X", "vat_number": "", "sdi_code": "", "pec": ""},
    "preferences": {"max_quantity_per_order": 1, "max_total_eur": 150, "payment_method": "PayPal", "confirm_on_telegram": True, "newsletter": False, "referral": "5"},
}
