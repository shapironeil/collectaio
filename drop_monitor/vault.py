"""Local encryption for payment data (card holder/number/expiry) stored in buyer profiles.

Key: PROFILE_KEY in .env (generated on first use). CVV is never stored: it is asked at checkout time.
This protects the files at rest on the PC; anyone with .env + personal/ can still decrypt.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from drop_monitor.profile import set_env_value

ENV_KEY = "PROFILE_KEY"
PREFIX = "enc:"


def _key(env_file: str | os.PathLike) -> bytes:
    key = os.environ.get(ENV_KEY, "")
    if not key:
        key = Fernet.generate_key().decode()
        set_env_value(env_file, ENV_KEY, key)
    return key.encode()


def encrypt(value: str, env_file: str | os.PathLike) -> str:
    if not value:
        return ""
    if value.startswith(PREFIX):
        return value
    return PREFIX + Fernet(_key(env_file)).encrypt(value.encode("utf-8")).decode()


def decrypt(value: str, env_file: str | os.PathLike) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""
    try:
        return Fernet(_key(env_file)).decrypt(value[len(PREFIX):].encode()).decode("utf-8")
    except InvalidToken:
        return ""


def mask_card(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 4:
        return ""
    return "•••• •••• •••• " + digits[-4:]


def card_brand(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    if d.startswith("4"):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", d):
        return "mastercard"
    if d.startswith(("34", "37")):
        return "amex"
    if d.startswith(("36", "38", "30")):
        return "diners"
    return "card"


def luhn_ok(number: str) -> bool:
    d = [int(c) for c in re.sub(r"\D", "", number or "")]
    if len(d) < 12:
        return False
    total = 0
    for i, n in enumerate(reversed(d)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0
