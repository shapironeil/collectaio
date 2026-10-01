"""Read and write config.yaml / .env from the window, keeping secrets out of YAML."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from drop_monitor.config import ConfigError, _expand_env, parse_config
from drop_monitor.profile import set_env_value

# (section, key) -> environment variable that holds the secret; YAML keeps "${VAR}"
SECRETS = {
    ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
    ("telegram", "chat_id"): "TELEGRAM_CHAT_ID",
    ("notify", "discord_webhook_url"): "DISCORD_WEBHOOK_URL",
}
EDITABLE_SECTIONS = ("site", "products", "polling", "telegram", "notify", "network", "tasks")


def read_raw(path: str | os.PathLike) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def public_config(path: str | os.PathLike) -> dict:
    """Raw config for the editor: secrets replaced by '' plus a has_<key> flag."""
    raw = read_raw(path)
    out = {k: raw.get(k) for k in EDITABLE_SECTIONS}
    out = yaml.safe_load(yaml.safe_dump(out)) or {}  # deep copy
    for (section, key), var in SECRETS.items():
        sec = out.setdefault(section, {}) or {}
        out[section] = sec
        value = str(sec.get(key, "") or "")
        resolved = _expand_env(value)
        sec[key] = ""
        sec[f"has_{key}"] = bool(resolved)
    out["path"] = str(path)
    return out


def save_config(path: str | os.PathLike, body: dict, env_path: str | os.PathLike) -> dict:
    """Merge `body` (editable sections) into config.yaml. Secrets go to .env. Validates before writing."""
    p = Path(path)
    raw = read_raw(p)
    for section in EDITABLE_SECTIONS:
        if section not in body:
            continue
        value = body[section]
        if section in ("products", "tasks"):
            raw[section] = _clean_list(value)
        else:
            current = raw.get(section) or {}
            if not isinstance(value, dict):
                raise ConfigError(f"'{section}' must be a mapping")
            merged = {**current, **{k: v for k, v in value.items() if not k.startswith("has_")}}
            if section == "network" and isinstance(merged.get("proxies"), list):
                merged["proxies"] = [str(x).strip() for x in merged["proxies"] if str(x).strip()]
            raw[section] = merged
    # secrets: non-empty -> .env, YAML keeps the placeholder; empty -> keep what is there
    env_updates: dict[str, str] = {}
    for (section, key), var in SECRETS.items():
        sec = raw.get(section) or {}
        if section not in raw:
            continue
        new_value = str(sec.get(key, "") or "")
        if new_value and not new_value.startswith("${"):
            env_updates[var] = new_value
        sec[key] = "${" + var + "}"
        raw[section] = sec
    # validate with the secrets applied
    env_backup = {k: os.environ.get(k) for k in env_updates}
    os.environ.update(env_updates)
    try:
        parse_config(_expand_env(yaml.safe_load(yaml.safe_dump(raw))), path=str(p))
    except ConfigError:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        raise
    for var, val in env_updates.items():
        set_env_value(env_path, var, val)
    header = "# drop-monitor config. Modificabile dalla finestra (drop-monitor app) o a mano. I segreti stanno in .env.\n"
    p.write_text(header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return public_config(p)


def _clean_list(items) -> list:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            d = {k: v for k, v in it.items() if v not in (None, "", []) or k in ("keywords", "name", "product")}
            out.append(d)
        elif isinstance(it, str) and it.strip():
            out.append(it.strip())
    return out
