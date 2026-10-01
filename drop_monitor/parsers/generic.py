"""Best-effort fallback for unknown shops: JSON-LD Product entries, then common card classes."""
from __future__ import annotations

import json

from bs4 import BeautifulSoup

from drop_monitor.models import Availability, ParseResult, Product
from drop_monitor.parsers.common import absolute, clean_text, parse_price


def parse(html: str, url: str, hint: str | None = None) -> ParseResult:
    soup = BeautifulSoup(html, "lxml")
    products = _from_json_ld(soup, url)
    if not products:
        products = _from_cards(soup, url)
    kind = hint if hint in ("product", "category", "search") else ("product" if len(products) == 1 else "category")
    return ParseResult(products=products, kind=kind)


def _from_json_ld(soup: BeautifulSoup, url: str) -> list[Product]:
    out: list[Product] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            if not isinstance(node, dict) or node.get("@type") not in ("Product", ["Product"]):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            avail = str(offers.get("availability", "")).lower()
            if "instock" in avail or "preorder" in avail:
                availability = Availability.AVAILABLE
            elif avail:
                availability = Availability.UNAVAILABLE
            else:
                availability = Availability.UNKNOWN
            price_raw = offers.get("price")
            price, value = parse_price(str(price_raw)) if price_raw is not None else (None, None)
            out.append(
                Product(
                    title=clean_text(node.get("name", "")),
                    url=absolute(url, node.get("url") or offers.get("url")) or url,
                    availability=availability,
                    price=price,
                    price_value=value,
                    product_id=str(node.get("sku") or node.get("productID") or "") or None,
                )
            )
    return [p for p in out if p.title]


def _walk(data):
    if isinstance(data, list):
        for x in data:
            yield from _walk(x)
    elif isinstance(data, dict):
        yield data
        for v in data.values():
            if isinstance(v, (list, dict)):
                yield from _walk(v)


def _from_cards(soup: BeautifulSoup, url: str) -> list[Product]:
    out: list[Product] = []
    for card in soup.select(".product-item, .product-card, .product, li.product, article.product"):
        a = card.select_one("h2 a, h3 a, .product-title a, a.product-title, a[href]")
        if a is None:
            continue
        title = clean_text(a.get("title") or a.get_text())
        href = absolute(url, a.get("href"))
        if not title or not href:
            continue
        price_el = card.select_one(".price, .actual-price, .amount")
        price, value = parse_price(price_el.get_text() if price_el else None)
        text = clean_text(card.get_text()).lower()
        if any(w in text for w in ("esaurito", "sold out", "out of stock", "non disponibile")):
            availability = Availability.UNAVAILABLE
        elif card.select_one("button, input[type=button], input[type=submit]"):
            availability = Availability.AVAILABLE
        else:
            availability = Availability.UNKNOWN
        out.append(Product(title=title, url=href, availability=availability, price=price, price_value=value))
    return out
