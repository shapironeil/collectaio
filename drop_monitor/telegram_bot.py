"""Background thread answering /status, /help, /ping on the configured chat."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from drop_monitor.notifier import STATE_ICON, STATE_LABEL, Telegram, _esc
from drop_monitor.store import Store

log = logging.getLogger(__name__)


def _ago(iso: str | None) -> str:
    if not iso:
        return "mai"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 90:
        return f"{secs}s fa"
    if secs < 5400:
        return f"{secs // 60}m fa"
    return f"{secs // 3600}h fa"


def build_status(store: Store, monitor_info: dict | None = None) -> str:
    lines = ["📊 <b>drop-monitor status</b>"]
    info = monitor_info or {}
    if info:
        lines.append(
            f"ciclo #{info.get('cycle', 0)} · ultimo OK {_ago(info.get('last_ok_at'))} · "
            f"errori consecutivi: {info.get('consecutive_errors', 0)}"
        )
        if info.get("backoff_until", 0) > time.time():
            lines.append(f"⏳ backoff per altri {int(info['backoff_until'] - time.time())}s")
        if info.get("last_error"):
            lines.append(f"⚠️ ultimo errore: {_esc(str(info['last_error'])[:200])}")
        if info.get("next_request"):
            lines.append(f"➡️ prossima richiesta: {_esc(info['next_request'])}")
    tracked = store.all_tracked()
    if not tracked:
        lines.append("Nessun prodotto ancora osservato.")
    for t in tracked:
        icon = STATE_ICON[t.state]
        title = t.title or t.label
        line = f"{icon} <b>{_esc(title)}</b>\n    {_esc(STATE_LABEL[t.state])}"
        if t.price:
            line += f" · {_esc(t.price)}"
        line += f" · visto {_ago(t.last_seen)} · check {_ago(t.last_checked)}"
        if t.url:
            line += f'\n    <a href="{_esc(t.url)}">pagina</a>'
        lines.append(line)
    lines.append(f"catalogo osservato: {store.seen_count()} prodotti")
    return "\n".join(lines)


class CommandBot(threading.Thread):
    def __init__(self, tg: Telegram, store: Store, info_provider, confirmer=None):
        super().__init__(name="telegram-commands", daemon=True)
        self._tg = tg
        self._store = store
        self._info = info_provider
        self._confirmer = confirmer
        self._stop = threading.Event()
        self._offset: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("telegram command bot started (chat %s)", self._tg.chat_id)
        while not self._stop.is_set():
            try:
                updates = self._tg.get_updates(self._offset, timeout=30)
            except Exception as e:  # network hiccups must not kill the thread
                log.warning("getUpdates failed: %s", e)
                self._stop.wait(10)
                continue
            for upd in updates:
                self._offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if cb:
                    chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                    if chat_id != self._tg.chat_id or self._confirmer is None:
                        self._tg.answer_callback(cb["id"], "non autorizzato")
                        continue
                    self._tg.answer_callback(cb["id"], self._confirmer.resolve(cb.get("data", "")))
                    continue
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if chat_id != self._tg.chat_id:
                    log.warning("ignoring command from unauthorized chat %s", chat_id)
                    continue
                self._handle(text.split()[0].split("@")[0].lower(), chat_id)

    def _handle(self, cmd: str, chat_id: str) -> None:
        if cmd == "/status":
            self._tg.send(build_status(self._store, self._info()), chat_id=chat_id, disable_preview=True)
        elif cmd == "/orders":
            rows = self._store.recent_orders(10)
            text = "🧾 <b>Ultimi ordini</b>\n" + ("\n".join(f"{_ago(r['ts'])} · {_esc(r['task'])}/{_esc(r['profile'])} · <b>{_esc(r['status'])}</b> {r['total_eur'] or ''}€ {_esc(r['note'] or '')}" for r in rows) if rows else "Nessun ordine ancora.")
            self._tg.send(text, chat_id=chat_id, disable_preview=True)
        elif cmd == "/ping":
            self._tg.send("pong 🏓", chat_id=chat_id)
        elif cmd in ("/help", "/start"):
            self._tg.send("Comandi: /status — stato prodotti e monitor\n/orders — ultimi ordini\n/ping — test\n/help — questo messaggio", chat_id=chat_id)
