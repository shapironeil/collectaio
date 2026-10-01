"""Shopify `/products.json` and `/collections/<handle>/products.json`."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from drop_monitor.models import Availability, ParseResult, Product
from drop_monitor.parsers.common import parse_price


def parse(text: str, url: str) -> ParseResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ParseResult(products=[], kind="shopify")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    products: list[Product] = []
    for p in data.get("products", []):
        variants = p.get("variants") or []
        available = any(v.get("available") for v in variants)
        first = variants[0] if variants else {}
        price, value = parse_price(str(first.get("price", ""))) if first else (None, None)
        handle = p.get("handle")
        variant_id = first.get("id")
        products.append(
            Product(
                title=p.get("title", ""),
                url=f"{base}/products/{handle}" if handle else base,
                availability=Availability.AVAILABLE if available else Availability.UNAVAILABLE,
                price=price,
                price_value=value,
                product_id=str(p.get("id")) if p.get("id") else None,
                add_to_cart_url=f"{base}/cart/{variant_id}:1" if variant_id else None,
                image_url=(p.get("images") or [{}])[0].get("src") if p.get("images") else None,
                extra={"listing_kind": "shopify"},
            )
        )
    return ParseResult(products=products, kind="shopify")
