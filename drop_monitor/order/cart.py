"""Shopping cart on nopCommerce 3.x: AJAX add, parse, clear, proceed to checkout."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from drop_monitor.order.session import ShopSession
from drop_monitor.parsers.common import clean_text, parse_price

CART_PATH = "/it/cart"


class CartError(Exception):
    pass


@dataclass
class CartItem:
    item_id: str
    name: str
    quantity: int
    unit_price: float | None
    subtotal: float | None


@dataclass
class CartView:
    items: list[CartItem] = field(default_factory=list)
    subtotal: float | None = None
    shipping: str | None = None
    total: float | None = None
    token: str | None = None
    has_terms_checkbox: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return sum(i.quantity for i in self.items)


def parse_cart(html: str) -> CartView:
    soup = BeautifulSoup(html, "lxml")
    view = CartView()
    form = soup.find("form", action=re.compile(r"/cart$", re.I)) or soup.find("form", action=re.compile(r"/cart", re.I))
    tok = form.find("input", attrs={"name": "__RequestVerificationToken"}) if form else None
    view.token = tok["value"] if tok else None
    view.has_terms_checkbox = bool(soup.find("input", attrs={"name": "termsofservice"}))
    for cb in soup.find_all("input", attrs={"name": "removefromcart"}):
        item_id = cb.get("value", "")
        row = cb.find_parent("tr")
        name_el = row.select_one(".product-name, .product a") if row else None
        qty_el = soup.find("input", attrs={"name": f"itemquantity{item_id}"})
        unit = row.select_one(".unit-price .product-unit-price, .product-unit-price") if row else None
        sub = row.select_one(".subtotal .product-subtotal, .product-subtotal") if row else None
        try:
            qty = int(qty_el.get("value", "1")) if qty_el else 1
        except ValueError:
            qty = 1
        view.items.append(CartItem(item_id, clean_text(name_el.get_text()) if name_el else "?", qty,
                                   parse_price(unit.get_text() if unit else None)[1], parse_price(sub.get_text() if sub else None)[1]))
    for tr in soup.select(".cart-total tr"):
        label = clean_text(tr.get_text(" ")).lower()
        value_el = tr.select_one(".cart-total-right, .value-summary, td:last-child")
        value_txt = clean_text(value_el.get_text()) if value_el else ""
        if "parziale" in label or "subtotal" in label:
            view.subtotal = parse_price(value_txt)[1]
        elif "spedizione" in label or "shipping" in label:
            view.shipping = value_txt
        elif label.startswith("totale") or label.startswith("total"):
            view.total = parse_price(value_txt)[1]
    view.warnings = [clean_text(w.get_text()) for w in soup.select(".message-error li, .warning-box li, .cart-item-row .message-error")]
    return view


class Cart:
    def __init__(self, session: ShopSession):
        self.s = session

    def add(self, product_id: str | int, quantity: int = 1) -> str:
        """AJAX add from catalog: /addproducttocart/catalog/{productId}/{cartTypeId=1}/{quantity}."""
        r = self.s.post(f"/it/addproducttocart/catalog/{product_id}/1/{int(quantity)}", ajax=True, referer=self.s.url("/it/"))
        try:
            j = r.json()
        except ValueError as e:
            raise CartError(f"risposta add-to-cart non JSON (HTTP {r.status_code})") from e
        if not j.get("success"):
            raise CartError(_strip_html(j.get("message") or "add-to-cart rifiutato"))
        return _strip_html(j.get("message") or "ok")

    def view(self) -> CartView:
        return parse_cart(self.s.get(CART_PATH).text)

    def clear(self) -> CartView:
        view = self.view()
        if not view.items:
            return view
        pairs = [("__RequestVerificationToken", view.token or "")]
        for it in view.items:
            pairs += [("removefromcart", it.item_id), (f"itemquantity{it.item_id}", str(it.quantity))]
        pairs.append(("updatecart", "Aggiorna carrello"))
        r = self.s.post(CART_PATH, content=urlencode(pairs), headers={"Content-Type": "application/x-www-form-urlencoded"}, referer=self.s.url(CART_PATH))
        return parse_cart(r.text)

    def proceed_to_checkout(self, view: CartView | None = None) -> str:
        """Accept terms and press 'checkout'. Returns the final URL (one-page checkout or login page)."""
        view = view or self.view()
        data = {"__RequestVerificationToken": view.token or "", "checkout": "checkout"}
        if view.has_terms_checkbox:
            data["termsofservice"] = "true"
        r = self.s.post(CART_PATH, data=data, referer=self.s.url(CART_PATH))
        return str(r.url)


def _strip_html(s: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", s or ""))
