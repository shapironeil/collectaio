"""config.yaml loading, `${ENV}` expansion and validation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from drop_monitor.matching import Watch
from drop_monitor.order.models import Task

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
    error_after_consecutive: int = 10  # warn after N consecutive fetch errors (0 = never)
    discord_webhook_url: str = ""  # optional Discord webhook (notifications only; confirmations stay on Telegram)


@dataclass
class NetworkConfig:
    proxies: list = field(default_factory=list)  # strings or {url, group, enabled, label}; see drop_monitor.proxies
    proxy_mode: str = "off"  # off | rotate (monitor: one proxy per request) | sticky (one proxy per buyer profile)
    monitor_group: str = ""  # proxy group used by the monitor ("" = all enabled)
    entries: list = field(default_factory=list)  # parsed ProxyEntry list (filled by parse_config)

    def urls(self, group: str = "") -> list[str]:
        from drop_monitor.proxies import enabled_urls

        return enabled_urls(self.entries, group)


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
    tasks: list[Task] = field(default_factory=list)
    track_product_pages: bool = True
    polling: PollingConfig = field(default_factory=PollingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
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


def load_dotenv(path: str | os.PathLike, override: bool = False) -> int:
    """Load KEY=VALUE lines from a .env file into os.environ. Returns the number of keys set.

    Docker Compose injects .env itself; this covers plain `drop-monitor run` on a PC.
    Existing environment variables win unless `override` is True.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    count = 0
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and (override or key not in os.environ):
            os.environ[key] = value
            count += 1
    return count


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    load_dotenv(p.parent / ".env")  # same folder as config.yaml
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
    network = _build(NetworkConfig, raw.get("network"), "network")
    if network.proxy_mode not in ("off", "rotate", "sticky"):
        raise ConfigError("network.proxy_mode must be off, rotate or sticky")
    from drop_monitor.proxies import ProxyFormatError, entries_from_config, parse_proxy_line

    network.entries = entries_from_config(network.proxies)
    for e in network.entries:
        try:
            e.url = parse_proxy_line(e.url)
        except ProxyFormatError as ex:
            raise ConfigError(f"network.proxies: {ex}")
    network.proxies = [e.url for e in network.entries]

    tasks: list[Task] = []
    labels = {w.label for w in watches} | {w.keywords for w in watches}
    for i, t in enumerate(raw.get("tasks") or []):
        task = _build(Task, t, f"tasks[{i}]")
        if task.product not in labels:
            raise ConfigError(f"tasks[{i}].product '{task.product}' does not match any products[].keywords/name")
        if task.mode not in ("monitor", "auto_checkout"):
            raise ConfigError(f"tasks[{i}].mode must be 'monitor' or 'auto_checkout'")
        if task.quantity < 1:
            raise ConfigError(f"tasks[{i}].quantity must be >= 1")
        if isinstance(task.profiles, str):
            task.profiles = [task.profiles]
        tasks.append(task)

    return Config(
        sources=sources,
        watches=watches,
        tasks=tasks,
        track_product_pages=bool(site.get("track_product_pages", True)),
        polling=polling,
        telegram=telegram,
        notify=_build(NotifyConfig, raw.get("notify"), "notify"),
        network=network,
        storage=_build(StorageConfig, raw.get("storage"), "storage"),
        path=path,
    )
