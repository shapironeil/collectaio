"""Heartbeat file written every cycle; `drop-monitor healthcheck` reads it (Docker HEALTHCHECK)."""
from __future__ import annotations

import json
import time
from pathlib import Path


def write_health(path: str, **fields) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"written_at": time.time(), **fields}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def check_health(path: str, max_age_seconds: int) -> tuple[bool, str]:
    p = Path(path)
    if not p.exists():
        return False, f"no heartbeat file at {p}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"unreadable heartbeat: {e}"
    age = time.time() - float(data.get("written_at", 0))
    if age > max_age_seconds:
        return False, f"heartbeat is {age:.0f}s old (max {max_age_seconds}s)"
    backoff_until = data.get("backoff_until") or 0
    # While backing off we are alive but idle: heartbeat is still refreshed by the loop.
    status = data.get("status", "unknown")
    if status == "stopped":
        return False, "monitor reported stopped"
    note = f" (backing off for {backoff_until - time.time():.0f}s)" if backoff_until > time.time() else ""
    return True, f"ok, last heartbeat {age:.0f}s ago, status={status}{note}"
