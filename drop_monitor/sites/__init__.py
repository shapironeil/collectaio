"""Site modules: one folder per shop, two procedures each (auth = login/registration, checkout = cart/order).

A module is *studied and saved*: `procedure` dictionaries document every endpoint, field and
signal verified on the shop, and the code classes implement them. The registry resolves a
module from a URL so the rest of the app never hard-codes a shop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit


@dataclass
class Procedure:
    """A documented step list for a site (what we learned), rendered in the window."""

    name: str  # "auth" | "checkout" | "monitor"
    title: str
    verified: str  # what was verified and when
    steps: list[dict] = field(default_factory=list)  # {step, method, path, notes}
    fields: dict[str, str] = field(default_factory=dict)  # site field -> profile key
    signals: dict[str, str] = field(default_factory=dict)  # name -> how it is detected
    limits: list[str] = field(default_factory=list)


@dataclass
class SiteModule:
    key: str
    domains: tuple[str, ...]
    platform: str
    title: str
    procedures: dict[str, Procedure]
    auth_factory: Callable | None = None  # (base_url, user_agent, timeout) -> AccountClient-like
    checkout_factory: Callable | None = None  # (base_url, user_agent, profiles, store, **kw) -> OrderRunner-like
    parser_hint: str = "auto"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "domains": list(self.domains), "platform": self.platform, "title": self.title,
            "parser_hint": self.parser_hint, "notes": self.notes,
            "procedures": {k: {"name": p.name, "title": p.title, "verified": p.verified, "steps": p.steps,
                               "fields": p.fields, "signals": p.signals, "limits": p.limits} for k, p in self.procedures.items()},
        }


_REGISTRY: dict[str, SiteModule] = {}


def register(module: SiteModule) -> SiteModule:
    _REGISTRY[module.key] = module
    return module


def all_modules() -> list[SiteModule]:
    _load()
    return list(_REGISTRY.values())


def module_for(url_or_host: str) -> SiteModule | None:
    _load()
    host = urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host
    host = host.lower()
    for m in _REGISTRY.values():
        if any(host == d or host.endswith("." + d) for d in m.domains):
            return m
    return None


def _load() -> None:
    if not _REGISTRY:
        from drop_monitor.sites import gemcard  # noqa: F401  (registers itself)
