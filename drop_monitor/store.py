"""SQLite persistence: tracked product state, events, run metadata, seen catalog."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from drop_monitor.models import State

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    watch_key   TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    state       TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    product_id  TEXT,
    price       TEXT,
    price_value REAL,
    add_to_cart_url TEXT,
    source_kind TEXT,
    source_url  TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    last_checked TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    watch_key TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    price     TEXT,
    note      TEXT
);
CREATE TABLE IF NOT EXISTS seen_products (
    key        TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    price      TEXT,
    availability TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Tracked:
    watch_key: str
    label: str
    state: State
    title: Optional[str] = None
    url: Optional[str] = None
    product_id: Optional[str] = None
    price: Optional[str] = None
    price_value: Optional[float] = None
    add_to_cart_url: Optional[str] = None
    source_kind: Optional[str] = None
    source_url: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    last_checked: Optional[str] = None
    updated_at: str = ""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- tracked products -------------------------------------------------
    def get_tracked(self, watch_key: str) -> Tracked | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracked WHERE watch_key = ?", (watch_key,)).fetchone()
        return self._row_to_tracked(row) if row else None

    def all_tracked(self) -> list[Tracked]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tracked ORDER BY label").fetchall()
        return [self._row_to_tracked(r) for r in rows]

    def upsert_tracked(self, t: Tracked) -> None:
        t.updated_at = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO tracked (watch_key,label,state,title,url,product_id,price,price_value,add_to_cart_url,
                       source_kind,source_url,first_seen,last_seen,last_checked,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(watch_key) DO UPDATE SET
                       label=excluded.label, state=excluded.state, title=excluded.title, url=excluded.url,
                       product_id=excluded.product_id, price=excluded.price, price_value=excluded.price_value,
                       add_to_cart_url=excluded.add_to_cart_url, source_kind=excluded.source_kind,
                       source_url=excluded.source_url, first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                       last_checked=excluded.last_checked, updated_at=excluded.updated_at""",
                (t.watch_key, t.label, t.state.value, t.title, t.url, t.product_id, t.price, t.price_value,
                 t.add_to_cart_url, t.source_kind, t.source_url, t.first_seen, t.last_seen, t.last_checked, t.updated_at),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_tracked(row: sqlite3.Row) -> Tracked:
        d = dict(row)
        d["state"] = State(d["state"])
        return Tracked(**d)

    # --- events -------------------------------------------------------------
    def add_event(self, watch_key: str, old_state: State | None, new_state: State, price: str | None, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, watch_key, old_state, new_state, price, note) VALUES (?,?,?,?,?,?)",
                (utcnow(), watch_key, old_state.value if old_state else None, new_state.value, price, note),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- seen catalog -------------------------------------------------------
    def touch_seen(self, key: str, title: str, url: str, price: str | None, availability: str) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO seen_products (key,title,url,price,availability,first_seen,last_seen)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET title=excluded.title, url=excluded.url, price=excluded.price,
                       availability=excluded.availability, last_seen=excluded.last_seen""",
                (key, title, url, price, availability, now, now),
            )
            self._conn.commit()

    def seen_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM seen_products").fetchone()[0])

    # --- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
