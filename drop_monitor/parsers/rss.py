"""RSS 2.0 / Atom product feeds (e.g. nopCommerce `/newproducts/rss`).

Feeds list new products with title + link only: availability is UNKNOWN and the
listing cannot tell us when a product disappears. Used for *discovery* only.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from drop_monitor.models import Availability, ParseResult, Product
from drop_monitor.parsers.common import clean_text

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse(text: str, url: str) -> ParseResult:
    products: list[Product] = []
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return ParseResult(products=[], kind="rss")
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        guid = clean_text(item.findtext("guid"))
        if title and link:
            pid = guid.rsplit(":", 1)[-1] if guid and ":" in guid else None
            products.append(Product(title=title, url=link, availability=Availability.UNKNOWN, product_id=pid, extra={"listing_kind": "rss"}))
    for entry in root.iter(f"{_ATOM}entry"):
        title = clean_text(entry.findtext(f"{_ATOM}title"))
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href") if link_el is not None else None
        if title and link:
            products.append(Product(title=title, url=link, availability=Availability.UNKNOWN, extra={"listing_kind": "rss"}))
    return ParseResult(products=products, kind="rss")
