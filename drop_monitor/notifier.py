"""Telegram Bot API client (sendMessage, getUpdates) over plain HTTPS."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import httpx

from drop_monitor.models import State
from drop_monitor.state import Transition

log = logging.getLogger(__name__)

STATE_LABEL = {
    State.ABSENT: "non presente",
    State.PRESENT_UNAVAILABLE: "presente, non acquistabile",
    State.AVAILABLE: "DISPONIBILE",
}
STATE_ICON = {
    State.ABSENT: "⚪️",
    State.PRESENT_UNAVAILABLE: "🟡",
    State.AVAILABLE: "🟢",
}


class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: float = 15):
        self.token = token
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, read=timeout + 35))

    def close(self) -> None:
        self._client.close()

    def send(self, text: str, chat_id: str | None = None, disable_preview: bool = False) -> bool:
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            r = self._client.post(f"{self._base}/sendMessage", json=payload)
            if r.status_code != 200:
                log.error("telegram sendMessage failed: HTTP %s %s", r.status_code, r.text[:300])
                return False
            return True
        except httpx.HTTPError as e:
            log.error("telegram sendMessage error: %s", e)
            return False

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        r = self._client.get(f"{self._base}/getUpdates", params=params)
        r.raise_for_status()
        data = r.json()
        return data.get("result", []) if data.get("ok") else []

    def get_me(self) -> dict:
        r = self._client.get(f"{self._base}/getMe")
        r.raise_for_status()
        return r.json().get("result", {})


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def format_transition(t: Transition) -> str:
    tr = t.tracked
    new, old = t.new_state, t.old_state
    icon = STATE_ICON[new]
    if new == State.AVAILABLE:
        headline = "DISPONIBILE ORA" if old == State.PRESENT_UNAVAILABLE else "NUOVO E DISPONIBILE"
    elif new == State.PRESENT_UNAVAILABLE:
        headline = "esaurito" if old == State.AVAILABLE else "comparso (non acquistabile)"
    else:
        headline = "rimosso dal sito"
    lines = [f"{icon} <b>{_esc(headline)}</b> — {_esc(tr.title or t.watch.label)}"]
    if tr.price:
        lines.append(f"💶 Prezzo: <b>{_esc(tr.price)}</b>")
    prev = STATE_LABEL[old] if old else "mai visto"
    lines.append(f"📦 Stato: {_esc(STATE_LABEL[new])} (prima: {_esc(prev)})")
    if tr.url:
        lines.append(f'🔗 <a href="{_esc(tr.url)}">Pagina prodotto</a>')
    if tr.add_to_cart_url:
        lines.append(f"🛒 Add-to-cart endpoint (POST): <code>{_esc(tr.add_to_cart_url)}</code>")
    lines.append(f"🔎 Chiave: {_esc(t.watch.label)} · fonte: {_esc(tr.source_kind or '?')}")
    lines.append(f"🕒 {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    return "\n".join(lines)


def format_price_change(t: Transition) -> str:
    tr = t.tracked
    return (
        f"💶 <b>Prezzo cambiato</b> — {_esc(tr.title or t.watch.label)}\n"
        f"{_esc(t.old_price)} → <b>{_esc(tr.price)}</b> ({_esc(STATE_LABEL[t.new_state])})\n"
        f'🔗 <a href="{_esc(tr.url or "")}">Pagina prodotto</a>'
    )
