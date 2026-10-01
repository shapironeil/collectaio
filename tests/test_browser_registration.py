"""Real headless Chromium against a local copy of the shop's registration form (no network, nothing submitted)."""
from __future__ import annotations

import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
pytest.importorskip("playwright")
if not Path(CHROME).exists():
    pytest.skip("Chromium not available", allow_module_level=True)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/it/register"):
            body = (FIX / "nop_register_form.html").read_bytes()
        elif self.path.startswith("/country/getstatesbycountryid"):
            body = json.dumps([{"id": 0, "name": "Seleziona"}, {"id": 130, "name": "Milano"}, {"id": 76, "name": "Agrigento"}]).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        else:
            body = b"<html><body><a href='/it/register?returnurl=%2fit%2fcart'>Registrati</a></body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


class FakeProfiles:
    def __init__(self, profile):
        self.profile = profile
        self.saved = []

    def load(self, name):
        return {**self.profile, "name": name}

    def save(self, name, data, password=None):
        self.saved.append((name, data, password))


@pytest.fixture
def site(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_browser_fills_the_form_and_stops_in_dry_run(site, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_PATH", CHROME)
    from drop_monitor.browser import DEMO_PROFILE, BrowserRegistrar

    prof = {**DEMO_PROFILE, "account": {**DEMO_PROFILE["account"], "password": "DemoPass123"}}
    reg = BrowserRegistrar(site, FakeProfiles(prof), tmp_path / "sessions", tmp_path / "shots", headless=True, slow_mo=0)
    st = reg.start("demo", None, dry_run=True, captcha_timeout=8)
    # the "person" answers the captcha from the window after a moment
    time.sleep(2.5)
    reg.answer_captcha("45")
    for _ in range(60):
        if st.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.5)
    steps = {s["step"]: s for s in st.steps}
    assert st.status == "done", st.steps
    assert steps["click Registrati"]["status"] == "ok" or "apertura modulo" in steps
    assert steps["campo FirstName"]["detail"] == "Mario" and steps["campo Email"]["detail"] == "mario.rossi.demo@example.com"
    assert steps["provincia"]["status"] == "ok" and steps["captcha"]["detail"] == "45"
    assert steps["dry-run"]["status"] == "ok" and st.result["dry_run"] is True
    assert len(st.screenshots) >= 4 and all(Path(p).exists() for p in st.screenshots)
    assert not reg.profiles.saved  # nothing persisted in dry-run
