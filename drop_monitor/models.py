"""Core data types shared by parsers, store and notifier."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Availability(str, Enum):
    """Observed availability of a product on a single page."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"  # source does not expose availability (e.g. RSS, search grid)


class State(str, Enum):
    """Tracked state of a watched product (persisted in SQLite)."""

    ABSENT = "absent"  # never seen / disappeared from the site
    PRESENT_UNAVAILABLE = "present_unavailable"  # listed but not purchasable
    AVAILABLE = "available"  # listed and purchasable


@dataclass
class Product:
    """A product as parsed from a page."""

    title: str
    url: str
    availability: Availability = Availability.UNKNOWN
    price: Optional[str] = None  # human readable, e.g. "€129,89"
    price_value: Optional[float] = None  # numeric, e.g. 129.89
    product_id: Optional[str] = None  # shop-side id (nopCommerce data-productid)
    add_to_cart_url: Optional[str] = None  # endpoint (often POST-only, see README)
    image_url: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        """Stable identity of the product on the shop: id when known, else URL."""
        return self.product_id or self.url


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str = ""
    elapsed: float = 0.0


@dataclass
class ParseResult:
    """Everything a parser could extract from one fetched page."""

    products: list[Product]
    kind: str  # "category" | "search" | "product" | "rss" | "shopify" | "sitemap"
    next_pages: list[str] = field(default_factory=list)  # other pages of a paginated listing
    page_number: int = 1
    total_pages: int = 1
