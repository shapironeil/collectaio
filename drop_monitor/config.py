"""config.yaml loading, `${ENV}` expansion and validation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from drop_monitor.matching import Watch

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SOURCE_TYPES = ("auto", "category", "search", "product", "rss", "shopify")


class ConfigError(ValueError):
    pass


@dataclass
class Source:
    url: str
    type: str = "auto"
    follow_pagination: bool = True
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or f"{self.type}:{self.url}"


@dataclass
class PollingConfig:
    min_seconds: float = 30
    max_seconds: float = 60
    timeout_seconds: float = 20
    backoff_base_seconds: float = 60
    backoff_max_seconds: float = 900
    respect_robots: bool = True
    user_agent: str = "drop-monitor/0.1 (+https://github.com/shapironeil/collectaio)"
    accept_language: str = "it-IT,it;q=0.9,en;q=0.5"
    hot_ratio: int = 2  # product-page polls per discovery (listing/feed) poll


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = True
    commands: bool = True  # long-poll getUpdates to answer /status


@dataclass
class NotifyConfig:
    on_price_change: bool = False
    startup_message: bool = True
    error_after_consecutive: int = 10  # warn on Telegram after N consecutive fetch errors (0 = never)


@dataclass
class StorageConfig:
    db_path: str = "data/drop-monitor.db"
    log_path: str = "data/drop-monitor.log"
    log_level: str = "INFO"
    health_path: str = "data/health.json"
    health_max_age_seconds: int = 300


@dataclass
class Config:
    sources: list[Source]
    watches: list[Watch]
    track_product_pages: bool = True
    polling: PollingConfig = field(default_factory=PollingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    path: str | None = None


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            val = os.environ.get(name)
            if val is None:
                if default is not None:
                    return default
                return ""
            return val
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _build(cls, data: dict | None, name: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    allowed = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)
    return parse_config(raw, path=str(p))


def parse_config(raw: dict, path: str | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("top level of config must be a mapping")
    site = raw.get("site") or {}
    sources_raw = site.get("sources")
    if not sources_raw:
        # Backwards-friendly shorthand: site.url -> single auto source
        if site.get("url"):
            sources_raw = [{"url": site["url"], "type": "auto"}]
        else:
            raise ConfigError("site.sources must list at least one URL to watch")
    sources: list[Source] = []
    for i, s in enumerate(sources_raw):
        if isinstance(s, str):
            s = {"url": s}
        src = _build(Source, s, f"site.sources[{i}]")
        if src.type not in SOURCE_TYPES:
            raise ConfigError(f"site.sources[{i}].type must be one of {SOURCE_TYPES}")
        if not src.url.startswith(("http://", "https://")):
            raise ConfigError(f"site.sources[{i}].url must be an absolute http(s) URL")
        sources.append(src)

    products_raw = raw.get("products")
    if not products_raw:
        raise ConfigError("'products' must list at least one product to watch")
    watches: list[Watch] = []
    for i, w in enumerate(products_raw):
        if isinstance(w, str):
            w = {"keywords": w}
        watch = _build(Watch, w, f"products[{i}]")
        if not watch.keywords or not watch.keywords.strip():
            raise ConfigError(f"products[{i}].keywords is required")
        watches.append(watch)

    polling = _build(PollingConfig, raw.get("polling"), "polling")
    if polling.min_seconds <= 0 or polling.max_seconds < polling.min_seconds:
        raise ConfigError("polling.min_seconds must be > 0 and <= polling.max_seconds")
    if polling.hot_ratio < 0:
        raise ConfigError("polling.hot_ratio must be >= 0")

    telegram = _build(TelegramConfig, raw.get("telegram"), "telegram")
    telegram.chat_id = str(telegram.chat_id or "")
    if telegram.enabled and (not telegram.bot_token or not telegram.chat_id):
        raise ConfigError("telegram.bot_token and telegram.chat_id are required (or set telegram.enabled: false)")

    return Config(
        sources=sources,
        watches=watches,
        track_product_pages=bool(site.get("track_product_pages", True)),
        polling=polling,
        telegram=telegram,
        notify=_build(NotifyConfig, raw.get("notify"), "notify"),
        storage=_build(StorageConfig, raw.get("storage"), "storage"),
        path=path,
    )
