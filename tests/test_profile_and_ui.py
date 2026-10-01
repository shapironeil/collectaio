import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from drop_monitor.config import Config, PollingConfig, Source, StorageConfig, TelegramConfig
from drop_monitor.matching import Watch
from drop_monitor.profile import ProfileStore, env_key, set_env_value
from drop_monitor.store import Store
from drop_monitor.ui.server import UIState, make_handler


def test_profile_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("ORDER_PASSWORD", raising=False)
    ps = ProfileStore(tmp_path / "personal", tmp_path / ".env")
    assert ps.names() == ["default"]
    d = ps.load()
    assert d["shipping"]["country_id"] == 46 and d["account"]["password"] == ""
    saved = ps.save("default", {"account": {"email": "a@b.it", "password": "x"}, "shipping": {"city": "Milano"}}, password="segreta")
    assert saved["account"]["email"] == "a@b.it" and saved["account"]["password"] == "segreta"
    text = (tmp_path / "personal" / "order-profile.yaml").read_text()
    assert "segreta" not in text and "password" not in text.split("account:")[1].split("shipping:")[0]
    assert "ORDER_PASSWORD='segreta'" in (tmp_path / ".env").read_text()
    pub = ps.public()
    assert pub["has_password"] is True and pub["account"]["password"] == ""
    # second profile with its own env key
    ps.save("nonna", {"account": {"email": "n@b.it"}}, password="pw2")
    assert ps.names() == ["default", "nonna"] and env_key("nonna") == "ORDER_PASSWORD_NONNA"
    assert ps.load("nonna")["account"]["password"] == "pw2"
    ps.delete("nonna")
    assert ps.names() == ["default"]
    with pytest.raises(ValueError):
        ps.delete("default")
    with pytest.raises(ValueError):
        ps.path("Bad Name!")


def test_set_env_value_updates_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nORDER_PASSWORD='old'\n")
    set_env_value(env, "ORDER_PASSWORD", "it's new")
    lines = env.read_text().splitlines()
    assert lines[0] == "TELEGRAM_BOT_TOKEN=abc" and lines[1].startswith("ORDER_PASSWORD='it'") and len(lines) == 2
    assert os.environ["ORDER_PASSWORD"] == "it's new"


class FakeAccount:
    form = None

    def __init__(self, *a, **k):
        from drop_monitor.account import parse_registration_form
        from pathlib import Path

        html = (Path(__file__).parent / "fixtures" / "nop_register_form.html").read_text(encoding="utf-8")
        self._form = parse_registration_form(html, "https://www.gemcardinfinitycollection.it/it/register?returnurl=%2fit%2fcart")
        self.submitted = None

    def start_registration(self, return_url="/it/cart"):
        self.form = self._form
        return self.form

    def captcha_image(self):
        return b"GIF89a", "image/gif"

    def refresh_captcha(self):
        pass

    def states(self, country_id):
        return [{"id": 130, "name": "Milano"}]

    def submit_registration(self, payload):
        from drop_monitor.account import RegistrationResult

        self.submitted = payload
        return RegistrationResult(ok=True, message="ok", final_url="https://www.gemcardinfinitycollection.it/it/registerresult/1", result_id="1")


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.delenv("ORDER_PASSWORD", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("x: 1")
    cfg = Config(
        sources=[Source(url="https://www.gemcardinfinitycollection.it/it/pokemon-30%C2%BA-anniversario", type="category")],
        watches=[Watch(keywords="Mini Tin Case")],
        polling=PollingConfig(), telegram=TelegramConfig(enabled=False),
        storage=StorageConfig(db_path=":memory:", health_path=str(tmp_path / "health.json")), path=str(cfg_path),
    )
    state = UIState(cfg, Store(":memory:"))
    state.account = FakeAccount()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "")

    yield call, state
    httpd.shutdown()
    httpd.server_close()


def test_ui_api_flow(ui, tmp_path):
    call, state = ui
    st, body, ct = call("/")
    assert st == 200 and b"collectaio" in body and "text/html" in ct
    st, body, _ = call("/api/status")
    j = json.loads(body)
    assert st == 200 and j["watches"][0]["keywords"] == "Mini Tin Case" and j["site"] == "https://www.gemcardinfinitycollection.it"
    # profiles
    st, body, _ = call("/api/profiles")
    assert json.loads(body)["profiles"][0]["name"] == "default"
    prof = {"account": {"email": "mario@example.com"}, "password": "segreta1",
            "shipping": {"first_name": "Mario", "last_name": "Rossi", "address1": "Via Roma 1", "zip": "20100", "city": "Milano", "province_id": 130, "country_id": 46, "phone": "333"}}
    st, body, _ = call("/api/profiles/default", "PUT", prof)
    assert st == 200 and json.loads(body)["has_password"] is True
    assert (tmp_path / ".env").read_text().startswith("ORDER_PASSWORD='segreta1'")
    st, body, _ = call("/api/profiles/bad%20name", "PUT", prof)
    assert st == 400
    # registration flow
    st, body, _ = call("/api/register/start", "POST", {})
    assert st == 200 and json.loads(body)["has_captcha"] is True
    st, body, ct = call("/api/register/captcha")
    assert st == 200 and body.startswith(b"GIF") and ct == "image/gif"
    st, body, _ = call("/api/states?country=46")
    assert json.loads(body)[0]["name"] == "Milano"
    st, body, _ = call("/api/register/preview", "POST", {"captcha_answer": ""})
    j = json.loads(body)
    assert j["ok"] is False and "captcha" in j["error"].lower()
    st, body, _ = call("/api/register/preview", "POST", {"captcha_answer": "x1y2"})
    j = json.loads(body)
    assert j["ok"] and j["payload"]["Password"] == "••••••" and j["payload"]["FirstName"] == "Mario"
    st, body, _ = call("/api/register/submit", "POST", {"captcha_answer": "x1y2", "newsletter": True})
    j = json.loads(body)
    assert j["ok"] and j["result_id"] == "1"
    assert state.account.submitted["Newsletter"] == "true" and state.account.submitted["Password"] == "segreta1"
    # cross-origin requests are refused
    st, _, _ = call("/api/register/submit", "POST", {}, headers={"Origin": "https://evil.example"})
    assert st == 403
