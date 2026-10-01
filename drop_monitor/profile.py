"""Buyer profiles (Cyber AIO style: one profile per account/address, reusable by many tasks).

Layout on disk (all git-ignored, preserved by setup-windows.bat):
  personal/order-profile.yaml      profile "default" (kept for backwards compatibility)
  personal/profiles/<name>.yaml    additional profiles
Passwords are never written to YAML: profile <name> reads ORDER_PASSWORD_<NAME> from .env
("default" also accepts ORDER_PASSWORD).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILE: dict[str, Any] = {
    "account": {"email": "", "password": ""},
    "shipping": {
        "first_name": "", "last_name": "", "company": "", "address1": "", "address2": "",
        "zip": "", "city": "", "province": "", "province_id": 0, "country": "Italia", "country_id": 46, "phone": "",
    },
    "billing": {"same_as_shipping": True, "fiscal_code": "", "vat_number": "", "sdi_code": "", "pec": ""},
    "payment": {"holder": "", "number": "", "expiry": "", "brand": ""},  # number encrypted at rest (vault), CVV never stored
    "preferences": {
        "max_quantity_per_order": 1, "max_total_eur": 150, "shipping_method": "", "payment_method": "",
        "confirm_on_telegram": True, "newsletter": False, "referral": "",
    },
}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def env_key(name: str) -> str:
    return "ORDER_PASSWORD" if name == "default" else "ORDER_PASSWORD_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def _merge(base: dict, override: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand(v):
    if isinstance(v, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


class ProfileStore:
    def __init__(self, personal_dir: str | os.PathLike, env_file: str | os.PathLike):
        self.dir = Path(personal_dir)
        self.env_file = Path(env_file)

    # ---- paths ------------------------------------------------------------
    def path(self, name: str) -> Path:
        if name == "default":
            return self.dir / "order-profile.yaml"
        if not _NAME_RE.match(name):
            raise ValueError("nome profilo non valido: usa lettere minuscole, numeri, - e _")
        return self.dir / "profiles" / f"{name}.yaml"

    def names(self) -> list[str]:
        names = ["default"]
        for p in sorted((self.dir / "profiles").glob("*.yaml")):
            if _NAME_RE.match(p.stem):
                names.append(p.stem)
        return names

    # ---- read/write -------------------------------------------------------
    def load(self, name: str = "default") -> dict:
        """Profile merged over defaults; password resolved from the environment."""
        p = self.path(name)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.is_file() else {}
        merged = _expand(_merge(DEFAULT_PROFILE, data))
        pw = merged["account"].get("password") or ""
        if not pw:
            pw = os.environ.get(env_key(name), "")
            if not pw and name == "default":
                pw = os.environ.get("ORDER_PASSWORD", "")
        merged["account"]["password"] = pw
        merged["name"] = name
        pay = merged.get("payment") or {}
        pay["number_enc"] = pay.get("number", "")
        pay["number"] = ""  # decrypted only on demand (card_number())
        merged["payment"] = pay
        return merged

    def card_number(self, name: str = "default") -> str:
        """Decrypted card number (for the checkout step only)."""
        from drop_monitor.vault import decrypt

        return decrypt(self.load(name).get("payment", {}).get("number_enc", ""), self.env_file)

    def save(self, name: str, data: dict, password: str | None = None) -> dict:
        from drop_monitor.vault import card_brand, encrypt

        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = dict(data)
        data.pop("name", None)
        data.pop("has_password", None)
        pay = dict(data.get("payment") or {})
        pay.pop("cvv", None)  # never stored
        pay.pop("masked", None)
        existing = self.load(name).get("payment", {}) if p.is_file() else {}
        number = str(pay.get("number") or "")
        if number and not number.startswith("enc:") and "•" not in number:
            pay["brand"] = card_brand(number)
            pay["number"] = encrypt(re.sub(r"\s", "", number), self.env_file)
        else:
            pay["number"] = existing.get("number_enc", "") if ("•" in number or not number) else number
            pay["brand"] = existing.get("brand", "") if not pay.get("brand") else pay["brand"]
        if pay.get("holder") is None:
            pay["holder"] = existing.get("holder", "")
        data["payment"] = pay
        merged = _merge(DEFAULT_PROFILE, data)
        merged["account"] = {k: v for k, v in merged["account"].items() if k not in ("password", "has_password", "save_password")}
        header = (
            f"# Profilo acquirente '{name}' di drop-monitor (modificabile dalla finestra: drop-monitor ui).\n"
            f"# La password NON e' salvata qui: sta in .env come {env_key(name)}.\n"
        )
        p.write_text(header + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if password:
            set_env_value(self.env_file, env_key(name), password)
        return self.load(name)

    def delete(self, name: str) -> None:
        if name == "default":
            raise ValueError("il profilo default non si puo' eliminare")
        p = self.path(name)
        if p.is_file():
            p.unlink()

    def public(self, name: str = "default") -> dict:
        from drop_monitor.vault import mask_card

        prof = self.load(name)
        prof["has_password"] = bool(prof["account"].get("password"))
        prof["account"]["password"] = ""
        pay = prof.get("payment") or {}
        number = self.card_number(name) if pay.get("number_enc") else ""
        prof["payment"] = {"holder": pay.get("holder", ""), "expiry": pay.get("expiry", ""), "brand": pay.get("brand", ""),
                           "masked": mask_card(number), "has_card": bool(number), "number": ""}
        return prof


def set_env_value(env_path: str | os.PathLike, key: str, value: str) -> None:
    """Create or update KEY='VALUE' in a .env file."""
    p = Path(env_path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    quoted = "'" + value.replace("'", "'\"'\"'") + "'"
    new_line = f"{key}={quoted}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value
