import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from drop_monitor.config import ConfigError, load_config
from drop_monitor.config_edit import public_config, save_config
from drop_monitor.fetcher import Fetcher
from drop_monitor.notify import DiscordWebhook, Notifier, html_to_discord

BASE_YAML = """site:
  sources:
    - type: category
      url: https://www.gemcardinfinitycollection.it/it/pokemon-30%C2%BA-anniversario
products:
  - keywords: "Mini Tin Case"
    exclude: [casuale]
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"
  enabled: false
storage:
  db_path: ":memory:"
"""


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "config.yaml").write_text(BASE_YAML, encoding="utf-8")
    return tmp_path


def test_public_config_masks_secrets(cfg_dir, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    pub = public_config(cfg_dir / "config.yaml")
    assert pub["telegram"]["bot_token"] == "" and pub["telegram"]["has_bot_token"] is True
    assert pub["telegram"]["has_chat_id"] is False and pub["products"][0]["keywords"] == "Mini Tin Case"


def test_save_config_writes_yaml_and_env(cfg_dir):
    body = {
        "polling": {"min_seconds": 20, "max_seconds": 35, "hot_ratio": 3},
        "telegram": {"enabled": True, "bot_token": "999:tok", "chat_id": "42", "commands": True},
        "notify": {"discord_webhook_url": "https://discord.com/api/webhooks/1/x", "on_price_change": True},
        "network": {"proxy_mode": "rotate", "proxies": ["http://u:p@1.2.3.4:8080", ""]},
        "products": [{"keywords": "Bundle 6 Buste", "exclude": []}, "Mini Tin Case"],
        "tasks": [{"name": "t1", "product": "Mini Tin Case", "profiles": ["default"], "mode": "auto_checkout", "max_total_eur": 150}],
    }
    out = save_config(cfg_dir / "config.yaml", body, cfg_dir / ".env")
    raw = yaml.safe_load((cfg_dir / "config.yaml").read_text())
    assert raw["telegram"]["bot_token"] == "${TELEGRAM_BOT_TOKEN}" and raw["notify"]["discord_webhook_url"] == "${DISCORD_WEBHOOK_URL}"
    env = (cfg_dir / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN='999:tok'" in env and "DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/1/x'" in env
    assert raw["polling"]["min_seconds"] == 20 and raw["network"]["proxies"] == ["http://u:p@1.2.3.4:8080"]
    assert out["telegram"]["has_bot_token"] and out["tasks"][0]["name"] == "t1"
    cfg = load_config(cfg_dir / "config.yaml")
    assert cfg.telegram.bot_token == "999:tok" and cfg.network.proxy_mode == "rotate" and cfg.tasks[0].mode == "auto_checkout"
    assert [w.keywords for w in cfg.watches] == ["Bundle 6 Buste", "Mini Tin Case"]


def test_save_config_rejects_invalid_and_keeps_file(cfg_dir):
    before = (cfg_dir / "config.yaml").read_text()
    with pytest.raises(ConfigError, match="min_seconds"):
        save_config(cfg_dir / "config.yaml", {"polling": {"min_seconds": 90, "max_seconds": 30}}, cfg_dir / ".env")
    with pytest.raises(ConfigError, match="proxies"):
        save_config(cfg_dir / "config.yaml", {"network": {"proxies": ["1.2.3.4:80"]}}, cfg_dir / ".env")
    with pytest.raises(ConfigError, match="bot_token"):
        save_config(cfg_dir / "config.yaml", {"telegram": {"enabled": True}}, cfg_dir / ".env")
    assert (cfg_dir / "config.yaml").read_text() == before and not (cfg_dir / ".env").exists()


def test_fetcher_rotates_proxies():
    f = Fetcher("ua", proxies=["http://a:1", "http://b:2"], proxy_mode="rotate")
    assert len(f._clients) == 2
    seq = [f._next_client() for _ in range(4)]
    assert seq[0] is seq[2] and seq[1] is seq[3] and seq[0] is not seq[1]
    f.close()
    f2 = Fetcher("ua", proxies=["http://a:1"], proxy_mode="off")
    assert len(f2._clients) == 1 and f2.proxies == []
    f2.close()


def test_discord_formatting_and_fanout(monkeypatch):
    assert html_to_discord('<b>DISPONIBILE</b> — x\n<a href="https://s/p">Pagina</a> <code>u</code> &amp;') == "**DISPONIBILE** — x\nPagina: https://s/p `u` &"
    sent = []

    class FakeTG:
        def send(self, text, **kw):
            sent.append(("tg", text))
            return True

        def close(self):
            pass

    dc = DiscordWebhook("https://discord.example/hook")
    monkeypatch.setattr(dc._client, "post", lambda url, json: (sent.append(("dc", json["content"])) or type("R", (), {"status_code": 204, "text": ""})()))
    n = Notifier(FakeTG(), dc)
    assert n.enabled and n.send("<b>ciao</b>")
    assert sent == [("tg", "<b>ciao</b>"), ("dc", "**ciao**")]
    assert Notifier(None, None).enabled is False


def test_app_controller_and_config_api(cfg_dir, monkeypatch):
    from drop_monitor.app import MonitorController
    from drop_monitor.store import Store
    from drop_monitor.ui.server import UIState, make_handler

    store = Store(":memory:")
    ctl = MonitorController(str(cfg_dir / "config.yaml"), store)
    # never hit the network: make the monitor loop a no-op that waits for stop
    monkeypatch.setattr("drop_monitor.scheduler.Monitor.step", lambda self: None)
    st = ctl.start(dry_run=True)
    assert st["running"] and st["dry_run"]
    cfg = load_config(cfg_dir / "config.yaml")
    state = UIState(cfg, store, controller=ctl)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, method="GET", body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    st, j = call("/api/status")
    assert j["controller"]["running"] is True
    st, j = call("/api/config")
    assert st == 200 and j["polling"] is None or "min_seconds" not in (j["polling"] or {})
    st, j = call("/api/config", "PUT", {"polling": {"min_seconds": 15, "max_seconds": 25}})
    assert st == 200 and j["polling"]["min_seconds"] == 15 and j["monitor_running"] is True
    st, j = call("/api/config", "PUT", {"polling": {"min_seconds": 50, "max_seconds": 25}})
    assert st == 400 and "min_seconds" in j["error"]
    st, j = call("/api/monitor/stop", "POST", {})
    assert st == 200 and j["running"] is False
    st, j = call("/api/monitor/start", "POST", {"dry_run": False})
    assert j["running"] is True and j["dry_run"] is False and ctl.cfg.polling.min_seconds == 15
    ctl.stop()
    httpd.shutdown()
    httpd.server_close()
