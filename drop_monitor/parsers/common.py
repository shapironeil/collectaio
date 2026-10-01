"""Helpers shared by the HTML parsers."""
from __future__ import annotations

import re
from urllib.parse import urljoin

_PRICE_RE = re.compile(r"(\d(?:[\d.,\s]*\d)?)")


def parse_price(text: str | None) -> tuple[str | None, float | None]:
    """'€129,89' -> ('€129,89', 129.89); '1.299,00' -> 1299.0; '24.69' -> 24.69."""
    if not text:
        return None, None
    clean = " ".join(text.split())
    m = _PRICE_RE.search(clean)
    if not m:
        return clean or None, None
    num = m.group(1).replace(" ", "")
    if "," in num:  # Italian format: thousands '.' and decimals ','
        num = num.replace(".", "").replace(",", ".")
    elif num.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", num):
        num = num.replace(".", "")  # '1.299' / '1.299.000' are thousands
    try:
        return clean, float(num)
    except ValueError:
        return clean, None


def absolute(base: str, href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(base, href.strip())


def clean_text(s: str | None) -> str:
    return " ".join((s or "").split())
