import os
from pathlib import Path

import pytest

from drop_monitor.config import ConfigError, load_config, parse_config
from drop_monitor.fetcher import _parse_retry_after
from drop_monitor.health import check_health, write_health

FIX = Path(__file__).parent / "fixtures"

MINIMAL = {
    "site": {"sources": [{"url": "https://shop.example/cat", "type": "category"}]},
    "products": ["Mini Tin Case", {"keywords": "Bundle 6 Buste", "exclude": ["jap"]}],
    "telegram": {"bot_token": "t", "chat_id": 123},
}


def test_parse_minimal_config():
    cfg = parse_config(MINIMAL)
    assert cfg.sources[0].type == "category"
    assert [w.keywords for w in cfg.watches] == ["Mini Tin Case", "Bundle 6 Buste"]
    assert cfg.watches[1].exclude == ["jap"]
    assert cfg.telegram.chat_id == "123"
    assert cfg.polling.min_seconds == 30 and cfg.polling.max_seconds == 60


def test_env_expansion_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "secret")
    p = tmp_path / "config.yaml"
    p.write_text(
        "site:\n  url: https://shop.example/cat\nproducts: [\"x\"]\n"
        "telegram:\n  bot_token: ${TG_TOKEN}\n  chat_id: ${TG_CHAT:-42}\npolling:\n  min_seconds: 5\n  max_seconds: 9\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.telegram.bot_token == "secret" and cfg.telegram.chat_id == "42"
    assert cfg.sources[0].type == "auto"


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda d: d.pop("products"), "products"),
        (lambda d: d["site"].pop("sources"), "site.sources"),
        (lambda d: d.update(polling={"min_seconds": 60, "max_seconds": 30}), "polling.min_seconds"),
        (lambda d: d.update(telegram={"enabled": True}), "telegram.bot_token"),
        (lambda d: d.update(polling={"bogus": 1}), "unknown key"),
        (lambda d: d["site"]["sources"].append({"url": "ftp://x", "type": "category"}), "absolute http"),
    ],
)
def test_config_errors(mutate, msg):
    import copy

    data = copy.deepcopy(MINIMAL)
    mutate(data)
    with pytest.raises(ConfigError, match=msg):
        parse_config(data)


def test_real_robots_txt_has_no_wildcard_block_so_we_are_allowed():
    import urllib.robotparser

    rp = urllib.robotparser.RobotFileParser()
    rp.parse((FIX / "robots.txt").read_text().splitlines())
    ua = "drop-monitor/0.1"
    assert rp.can_fetch(ua, "https://www.gemcardinfinitycollection.it/it/pokemon-30%C2%BA-anniversario")
    # SmartSearch is disallowed for Googlebot only; we still prefer the category page (see README).
    assert not rp.can_fetch("Googlebot", "https://www.gemcardinfinitycollection.it/it/SmartSearch/Search?q=x")
    assert rp.can_fetch(ua, "https://www.gemcardinfinitycollection.it/it/SmartSearch/Search?q=x")


def test_retry_after_parsing():
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("garbage") is None


def test_healthcheck_roundtrip(tmp_path):
    path = tmp_path / "health.json"
    assert check_health(str(path), 60)[0] is False
    write_health(str(path), status="running", backoff_until=0)
    ok, msg = check_health(str(path), 60)
    assert ok and "status=running" in msg
    write_health(str(path), status="stopped")
    assert check_health(str(path), 60)[0] is False
