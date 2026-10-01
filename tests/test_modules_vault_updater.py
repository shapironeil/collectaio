import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from drop_monitor.sites import all_modules, module_for
from drop_monitor.vault import card_brand, decrypt, encrypt, luhn_ok, mask_card


def test_site_registry_resolves_by_host():
    m = module_for("https://www.gemcardinfinitycollection.it/it/x?y=1")
    assert m and m.key == "gemcard" and set(m.procedures) == {"auth", "checkout", "monitor"}
    assert module_for("https://other.example/") is None
    d = m.to_dict()
    assert d["procedures"]["auth"]["steps"][0]["path"].startswith("/it/register")
    assert "FirstName" in d["procedures"]["auth"]["fields"] and len(all_modules()) == 1


def test_vault_roundtrip_and_helpers(tmp_path, monkeypatch):
    monkeypatch.delenv("PROFILE_KEY", raising=False)
    env = tmp_path / ".env"
    token = encrypt("4111111111111111", env)
    assert token.startswith("enc:") and "4111" not in token
    assert "PROFILE_KEY=" in env.read_text()
    assert decrypt(token, env) == "4111111111111111"
    assert encrypt(token, env) == token and decrypt("", env) == "" and decrypt("enc:garbage", env) == ""
    assert mask_card("4111 1111 1111 1111") == "•••• •••• •••• 1111" and mask_card("12") == ""
    assert card_brand("4111") == "visa" and card_brand("5500") == "mastercard" and card_brand("371449") == "amex" and card_brand("9") == "card"
    assert luhn_ok("4111 1111 1111 1111") and not luhn_ok("4111 1111 1111 1112")


def test_profile_card_storage(tmp_path, monkeypatch):
    monkeypatch.delenv("PROFILE_KEY", raising=False)
    from drop_monitor.profile import ProfileStore

    ps = ProfileStore(tmp_path / "personal", tmp_path / ".env")
    ps.save("default", {"payment": {"holder": "MARIO ROSSI", "number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"}})
    text = (tmp_path / "personal" / "order-profile.yaml").read_text()
    assert "4111" not in text and "cvv" not in text and "enc:" in text
    pub = ps.public()
    assert pub["payment"]["masked"].endswith("1111") and pub["payment"]["brand"] == "visa" and pub["payment"]["has_card"]
    assert ps.card_number() == "4111111111111111"
    ps.save("default", {"payment": {"holder": "MARIO ROSSI", "number": pub["payment"]["masked"], "expiry": "01/30"}})
    assert ps.card_number() == "4111111111111111" and ps.public()["payment"]["expiry"] == "01/30"


def test_updater_check_and_apply(tmp_path, monkeypatch):
    from drop_monitor import updater

    root = tmp_path / "install"
    (root / "drop_monitor").mkdir(parents=True)
    (root / "drop_monitor" / "old.py").write_text("old")
    (root / "personal").mkdir()
    (root / "personal" / "keep.yaml").write_text("mine")
    (root / "config.yaml").write_text("keep: 1")
    (root / "install-info.txt").write_text("commit=aaaaaaa\nbranch=main\n")
    (root / "requirements.txt").write_text("")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("owner-repo-bbbbbbb/drop_monitor/old.py", "new")
        z.writestr("owner-repo-bbbbbbb/drop_monitor/new.py", "added")
        z.writestr("owner-repo-bbbbbbb/config.yaml", "overwritten: 1")
        z.writestr("owner-repo-bbbbbbb/personal/keep.yaml", "overwritten")

    class R:
        def __init__(self, status, content=b"", text="", j=None):
            self.status_code, self.content, self.text, self._j = status, content, text, j

        def json(self):
            return self._j

    def fake_get(url, **kw):
        if url.endswith("/commits/main"):
            return R(200, j={"sha": "bbbbbbb123", "commit": {"message": "Fix things\nmore", "committer": {"date": "2026-10-01"}}})
        if "/contents/" in url:
            return R(200, text='__version__ = "0.2.0"')
        if "/zipball/" in url:
            return R(200, content=buf.getvalue() + b"\0" * 20000)
        return R(404)

    u = updater.Updater(root=root)
    monkeypatch.setattr(u._client, "get", fake_get)
    info = u.check()
    assert info["ok"] and info["update_available"] and info["latest"] == "bbbbbbb" and info["remote_version"] == "0.2.0" and info["branch"] == "main"
    res = u.apply(install_requirements=False)
    assert res["ok"] and res["changed"] == 2 and res["restart_required"]
    assert (root / "drop_monitor" / "old.py").read_text() == "new" and (root / "drop_monitor" / "new.py").read_text() == "added"
    assert (root / "config.yaml").read_text() == "keep: 1" and (root / "personal" / "keep.yaml").read_text() == "mine"
    assert "commit=bbbbbbb" in (root / "install-info.txt").read_text()
    assert any(p.is_dir() for p in (root / "_backup").iterdir())


def test_task_cap_and_schedule(tmp_path, monkeypatch):
    from drop_monitor.order.models import Task
    from drop_monitor.order.runner import OrderRunner
    from drop_monitor.store import Store

    store = Store(":memory:")
    store.add_order("t", "default", "x", "placed", 10.0, None, "")
    r = OrderRunner("https://shop.example", "ua", None, store)
    (res,) = r.run_task(Task(name="t", product="x", profiles=["default"], max_checkouts=1), "1", "x")
    assert res.status == "capped"
    from drop_monitor.app import MonitorController

    (tmp_path / "config.yaml").write_text("site:\n  url: https://shop.example/c\nproducts: [x]\ntelegram:\n  enabled: false\nstorage:\n  db_path: ':memory:'\n")
    ctl = MonitorController(str(tmp_path / "config.yaml"), Store(":memory:"))
    monkeypatch.setattr("drop_monitor.scheduler.Monitor.step", lambda self: None)
    st = ctl.schedule("2099-01-01T10:00", dry_run=True)
    assert st["scheduled_at"] == "2099-01-01T10:00" and not st["running"]
    st = ctl.schedule(None)
    assert st["scheduled_at"] is None
    st = ctl.schedule("2000-01-01T10:00", dry_run=True)  # past -> starts now
    assert st["running"]
    ctl.stop()
