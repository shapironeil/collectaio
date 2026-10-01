"""Composite notifier: Telegram and/or Discord webhook. Confirmations (buttons) stay on Telegram."""
from __future__ import annotations

import logging
import re

import httpx

from drop_monitor.notifier import Telegram

log = logging.getLogger(__name__)


def html_to_discord(text: str) -> str:
    t = re.sub(r"<a href=\"([^\"]+)\">([^<]*)</a>", r"\2: \1", text)
    t = re.sub(r"</?b>", "**", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class DiscordWebhook:
    def __init__(self, url: str, timeout: float = 15):
        self.url = url
        self._client = httpx.Client(timeout=httpx.Timeout(timeout))

    def send(self, text: str, **_) -> bool:
        try:
            r = self._client.post(self.url, json={"content": html_to_discord(text)[:1900], "username": "drop-monitor"})
            if r.status_code >= 300:
                log.error("discord webhook failed: HTTP %s %s", r.status_code, r.text[:200])
                return False
            return True
        except httpx.HTTPError as e:
            log.error("discord webhook error: %s", e)
            return False

    def close(self) -> None:
        self._client.close()


class Notifier:
    """Fan-out to every configured channel. `telegram` is exposed for confirmations/commands."""

    def __init__(self, telegram: Telegram | None = None, discord: DiscordWebhook | None = None):
        self.telegram = telegram
        self.discord = discord

    @property
    def enabled(self) -> bool:
        return bool(self.telegram or self.discord)

    def send(self, text: str, **kw) -> bool:
        ok = False
        if self.telegram:
            ok = self.telegram.send(text, **kw) or ok
        if self.discord:
            ok = self.discord.send(text) or ok
        return ok

    def close(self) -> None:
        if self.telegram:
            self.telegram.close()
        if self.discord:
            self.discord.close()


def build_notifier(cfg) -> Notifier:
    tg = Telegram(cfg.telegram.bot_token, cfg.telegram.chat_id) if cfg.telegram.enabled else None
    dc = DiscordWebhook(cfg.notify.discord_webhook_url) if cfg.notify.discord_webhook_url else None
    return Notifier(tg, dc)
