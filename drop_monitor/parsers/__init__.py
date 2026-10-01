"""Page parsers. `parse()` picks the right parser for a fetched page."""
from __future__ import annotations

from drop_monitor.models import FetchResult, ParseResult
from drop_monitor.parsers import generic, nopcommerce, rss, shopify


def parse(fetched: FetchResult, hint: str | None = None) -> ParseResult:
    """Dispatch on content type / hint / markup fingerprint.

    `hint` is the source `type` from config ("category", "search", "product", "rss",
    "shopify", "auto").
    """
    ctype = (fetched.content_type or "").lower()
    text = fetched.text

    if hint == "rss" or "rss+xml" in ctype or "atom+xml" in ctype or text.lstrip().startswith("<?xml") and "<rss" in text[:2000]:
        return rss.parse(text, fetched.url)
    if hint == "shopify" or (ctype.startswith("application/json") and '"products"' in text[:200]):
        return shopify.parse(text, fetched.url)
    if nopcommerce.looks_like(text):
        return nopcommerce.parse(text, fetched.url, hint=hint)
    return generic.parse(text, fetched.url, hint=hint)
