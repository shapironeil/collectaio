"""Parser for nopCommerce storefronts (DefaultClean theme and close derivatives).

Verified on gemcardinfinitycollection.it (Oct 2026). Signals:

Listing pages (category / SmartSearch grid)
  * each product is `<div class="product-item" data-productid="N">`
  * title/link in `h2.product-title a`
  * price in `span.price.actual-price` (optional `span.old-price`)
  * the theme adds an absolutely positioned overlay `<div ...>Esaurito</div>` when the
    product is out of stock. The "Acquista" button is rendered regardless, so it is
    NOT an availability signal. Only the category grid renders the overlay: SmartSearch
    results do not, hence availability there is UNKNOWN.
  * pagination via `div.pager` links with `?pagenumber=N` (category) or
    `selectedPage=N` (SmartSearch).

Product page
  * `<div class="product-name"><h1>` title
  * `div.product-price span[id^=price-value-]` and `<meta itemprop="price">`
  * `div.back-in-stock-subscription` ("Inviami una mail quando ritorna disponibile") is
    present ONLY when stock <= 0  -> UNAVAILABLE; absent -> AVAILABLE.
  * `<link itemprop="availability" href="schema.org/InStock">` is ALWAYS InStock on this
    shop, so it is ignored.
  * add to cart is an AJAX POST to `/{lang}/addproducttocart/details/{id}/1`
    (listing: `/{lang}/addproducttocart/catalog/{id}/1/1`). There is no GET deep link.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from drop_monitor.models import Availability, ParseResult, Product
from drop_monitor.parsers.common import absolute, clean_text, parse_price

OUT_OF_STOCK_WORDS = ("esaurito", "sold out", "out of stock", "non disponibile", "terminato")
_CART_CATALOG_RE = re.compile(r"addproducttocart/catalog/(\d+)/1/\d+")
_CART_DETAILS_RE = re.compile(r"(/[^'\"]*addproducttocart/details/(\d+)/1)")
_PAGE_RE = re.compile(r"(?:pagenumber|selectedPage)=(\d+)", re.I)


def looks_like(html: str) -> bool:
    head = html[:200_000]
    return (
        'class="product-item"' in head and "data-productid" in head
    ) or "addproducttocart" in head or "product-details-form" in head


def parse(html: str, url: str, hint: str | None = None) -> ParseResult:
    soup = BeautifulSoup(html, "lxml")
    if hint == "product" or (hint in (None, "auto") and soup.find(id="product-details-form")):
        product = parse_product_page(soup, url)
        return ParseResult(products=[product] if product else [], kind="product")
    kind = "search" if "smartsearch" in url.lower() or hint == "search" else "category"
    products = parse_listing(soup, url, kind=kind)
    next_pages, page_no, total = parse_pagination(soup, url)
    return ParseResult(products=products, kind=kind, next_pages=next_pages, page_number=page_no, total_pages=total)


def parse_listing(soup: BeautifulSoup, base_url: str, kind: str = "category") -> list[Product]:
    out: list[Product] = []
    for item in soup.select("div.product-item"):
        pid = item.get("data-productid")
        a = item.select_one("h2.product-title a") or item.select_one(".product-title a")
        if a is None:
            continue
        title = clean_text(a.get_text())
        href = absolute(base_url, a.get("href"))
        if not title or not href:
            continue
        price_el = item.select_one("span.actual-price") or item.select_one(".prices .price")
        price, value = parse_price(price_el.get_text() if price_el else None)
        availability = _listing_availability(item, kind)
        cart = None
        m = _CART_CATALOG_RE.search(str(item))
        if m:
            onclick = item.find(attrs={"onclick": _CART_CATALOG_RE})
            if onclick is not None:
                path = re.search(r"'(/[^']*addproducttocart/catalog/[^']*)'", onclick["onclick"])
                cart = absolute(base_url, path.group(1)) if path else None
        img = item.select_one(".picture img")
        out.append(
            Product(
                title=title,
                url=href,
                availability=availability,
                price=price,
                price_value=value,
                product_id=pid,
                add_to_cart_url=cart,
                image_url=absolute(base_url, img.get("src")) if img else None,
                extra={"listing_kind": kind},
            )
        )
    return out


def _listing_availability(item, kind: str) -> Availability:
    # Theme overlay: a div with inline absolute positioning whose text is "Esaurito".
    for div in item.find_all("div", recursive=False):
        style = (div.get("style") or "").replace(" ", "").lower()
        text = clean_text(div.get_text()).lower()
        if "position:absolute" in style and any(w in text for w in OUT_OF_STOCK_WORDS):
            return Availability.UNAVAILABLE
    # Standard nopCommerce stock badge, if the theme renders one.
    badge = item.select_one(".stock, .availability, .product-availability, .sold-out, .out-of-stock")
    if badge is not None:
        text = clean_text(badge.get_text()).lower()
        if any(w in text for w in OUT_OF_STOCK_WORDS):
            return Availability.UNAVAILABLE
        if text:
            return Availability.AVAILABLE
    if kind == "search":
        # SmartSearch grid carries no stock information on this shop.
        return Availability.UNKNOWN
    return Availability.AVAILABLE


def parse_product_page(soup: BeautifulSoup, url: str) -> Product | None:
    name = soup.select_one("div.product-name h1") or soup.select_one("h1")
    if name is None:
        return None
    title = clean_text(name.get_text())
    root = soup.find(attrs={"data-productid": True})
    pid = root.get("data-productid") if root else None

    price_el = soup.select_one("div.product-price span[id^=price-value-]") or soup.select_one(".product-price span")
    price, value = parse_price(price_el.get_text() if price_el else None)
    if value is None:
        meta = soup.find("meta", attrs={"itemprop": "price"})
        if meta and meta.get("content"):
            try:
                value = float(meta["content"])
                price = price or f"€{value:.2f}".replace(".", ",")
            except ValueError:
                pass

    if soup.select_one("div.back-in-stock-subscription"):
        availability = Availability.UNAVAILABLE
    else:
        stock = soup.select_one("div.stock .value, div.availability .value")
        text = clean_text(stock.get_text()).lower() if stock else ""
        if any(w in text for w in OUT_OF_STOCK_WORDS):
            availability = Availability.UNAVAILABLE
        elif soup.select_one("input.add-to-cart-button"):
            availability = Availability.AVAILABLE
        else:
            availability = Availability.UNAVAILABLE

    cart = None
    m = _CART_DETAILS_RE.search(str(soup))
    if m:
        cart = absolute(url, m.group(1))
    canonical = soup.find("link", rel="canonical")
    page_url = canonical.get("href") if canonical and canonical.get("href") else url
    img = soup.select_one(".product-essential .picture img, .gallery img")
    return Product(
        title=title,
        url=page_url,
        availability=availability,
        price=price,
        price_value=value,
        product_id=pid,
        add_to_cart_url=cart,
        image_url=absolute(url, img.get("src")) if img else None,
        extra={"listing_kind": "product"},
    )


def parse_pagination(soup: BeautifulSoup, base_url: str) -> tuple[list[str], int, int]:
    """Return (other page URLs, current page number, total pages)."""
    pager = soup.select_one("div.pager")
    current = 1
    q = parse_qs(urlparse(base_url).query)
    for key in ("pagenumber", "selectedPage"):
        if key in q:
            try:
                current = int(q[key][0])
            except ValueError:
                pass
    if pager is None:
        return [], current, 1
    cur = pager.select_one(".current-page")
    if cur is not None and cur.get_text(strip=True).isdigit():
        current = int(cur.get_text(strip=True))
    pages: dict[int, str] = {current: base_url}
    for a in pager.select("a[href]"):
        href = absolute(base_url, a.get("href"))
        if not href:
            continue
        text = a.get_text(strip=True)
        m = _PAGE_RE.search(href)
        if text.isdigit():  # numbered link; page 1 usually has no page parameter at all
            pages[int(text)] = href
        elif m:
            pages[int(m.group(1))] = href
    total = max(pages) if pages else 1
    others = [pages[n] for n in sorted(pages) if n != current]
    return others, current, total
