"""nopCommerce 3.x one-page checkout (OPC) driver.

The page `/it/onepagecheckout` embeds sections (billing, shipping, shipping_method,
payment_method, payment_info, confirm_order) and inline `X.init('#co-X-form', 'saveUrl')`
calls (public.onepagecheckout.js, verified on the shop). Every step is a POST of the
section form to its saveUrl; the JSON answer carries either
  {"update_section": {"name": "shipping-method", "html": "..."}, "goto_section": "shipping_method", "allow_sections": [...]}
or {"error": 1, "message": "..."} or {"redirect": "..."} (external payment / completed page).
Nothing in this module places an order: `confirm()` is the only call that does, and the
runner only invokes it after the limits and the human confirmation have passed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from drop_monitor.order.session import ShopSession
from drop_monitor.parsers.common import clean_text, parse_price

OPC_PATH = "/it/onepagecheckout"
DEFAULT_URLS = {
    "billing": "/it/checkout/OpcSaveBilling/",
    "shipping": "/it/checkout/OpcSaveShipping/",
    "shipping_method": "/it/checkout/OpcSaveShippingMethod/",
    "payment_method": "/it/checkout/OpcSavePaymentMethod/",
    "payment_info": "/it/checkout/OpcSavePaymentInfo/",
    "confirm_order": "/it/checkout/OpcConfirmOrder/",
}
_INIT_RE = re.compile(r"(Billing|Shipping|ShippingMethod|PaymentMethod|PaymentInfo)\.init\(\s*'[^']*'\s*,\s*'([^']+)'", re.S)
_CONFIRM_RE = re.compile(r"ConfirmOrder\.init\(\s*'([^']+)'", re.S)  # ConfirmOrder.init(saveUrl, failureUrl)
_INIT_KEY = {"Billing": "billing", "Shipping": "shipping", "ShippingMethod": "shipping_method", "PaymentMethod": "payment_method", "PaymentInfo": "payment_info", "ConfirmOrder": "confirm_order"}


class CheckoutError(Exception):
    pass


@dataclass
class Option:
    value: str
    label: str
    price: float | None = None
    selected: bool = False


@dataclass
class StepResponse:
    section: str | None = None  # name of the section to show next (goto_section)
    html: str = ""  # html of the updated section
    redirect: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class OrderSummary:
    items: list[tuple[str, int, float | None]] = field(default_factory=list)
    subtotal: float | None = None
    shipping: float | None = None
    total: float | None = None
    shipping_method: str | None = None
    payment_method: str | None = None
    warnings: list[str] = field(default_factory=list)


class OnePageCheckout:
    def __init__(self, session: ShopSession):
        self.s = session
        self.urls = dict(DEFAULT_URLS)
        self.addresses: list[Option] = []
        self.last: StepResponse | None = None

    # ---- page ------------------------------------------------------------
    def start(self) -> str:
        r = self.s.get(OPC_PATH)
        if "/login" in str(r.url).lower():
            raise CheckoutError("checkout richiede il login (sessione scaduta?)")
        html = r.text
        for m in _INIT_RE.finditer(html):
            self.urls[_INIT_KEY[m.group(1)]] = m.group(2)
        m = _CONFIRM_RE.search(html)
        if m:
            self.urls["confirm_order"] = m.group(1)
        soup = BeautifulSoup(html, "lxml")
        self.addresses = [
            Option(o.get("value", ""), clean_text(o.get_text()), selected=o.has_attr("selected"))
            for o in soup.select("#billing-address-select option") if o.get("value")
        ]
        self._billing_form = soup.select_one("#co-billing-form")
        self._billing_html = html
        return html

    # ---- steps -------------------------------------------------------------
    def save_billing(self, profile: dict, address_id: str | None = None) -> StepResponse:
        """Pick an existing address (address_id) or post a new one from the profile. Ship to same address."""
        data: dict[str, str] = {"ShipToSameAddress": "true"}
        if address_id:
            data["billing_address_id"] = address_id
        else:
            data["billing_address_id"] = ""
            sh, acc = profile.get("shipping", {}), profile.get("account", {})
            data.update({
                "BillingNewAddress.FirstName": sh.get("first_name", ""),
                "BillingNewAddress.LastName": sh.get("last_name", ""),
                "BillingNewAddress.Email": acc.get("email", ""),
                "BillingNewAddress.Company": sh.get("company", ""),
                "BillingNewAddress.CountryId": str(sh.get("country_id") or 0),
                "BillingNewAddress.StateProvinceId": str(sh.get("province_id") or 0),
                "BillingNewAddress.City": sh.get("city", ""),
                "BillingNewAddress.Address1": sh.get("address1", ""),
                "BillingNewAddress.Address2": sh.get("address2", ""),
                "BillingNewAddress.ZipPostalCode": str(sh.get("zip", "")),
                "BillingNewAddress.PhoneNumber": str(sh.get("phone", "")),
                "BillingNewAddress.FaxNumber": "",
            })
        data.update(self._hidden(self._billing_form))
        return self._post("billing", data)

    def choose_address(self, profile: dict) -> str | None:
        """Existing address whose text contains the profile's street and zip, if any."""
        sh = profile.get("shipping", {})
        key = (sh.get("address1") or "").lower()
        zipc = str(sh.get("zip") or "")
        for a in self.addresses:
            txt = a.label.lower()
            if key and key in txt and (not zipc or zipc in txt):
                return a.value
        return None

    def options(self, html: str, name: str) -> list[Option]:
        soup = BeautifulSoup(html, "lxml")
        out = []
        for inp in soup.find_all("input", attrs={"name": name}):
            label_el = soup.find("label", attrs={"for": inp.get("id")}) if inp.get("id") else None
            label = clean_text(label_el.get_text()) if label_el else inp.get("value", "")
            if not label_el:  # payment methods: label may be in the sibling block
                parent = inp.find_parent("li") or inp.find_parent("div")
                label = clean_text(parent.get_text(" ")) if parent else label
            price = parse_price(label)[1] if "€" in label else None
            out.append(Option(inp.get("value", ""), label, price, inp.has_attr("checked")))
        return out

    def save_shipping_method(self, html: str, preference: str = "") -> tuple[StepResponse, Option]:
        opts = self.options(html, "shippingoption")
        if not opts:
            raise CheckoutError("nessun metodo di spedizione offerto")
        opt = _pick(opts, preference)
        data = {"shippingoption": opt.value}
        data.update(self._hidden(BeautifulSoup(html, "lxml")))
        return self._post("shipping_method", data), opt

    def save_payment_method(self, html: str, preference: str = "") -> tuple[StepResponse, Option]:
        opts = self.options(html, "paymentmethod")
        if not opts:
            raise CheckoutError("nessun metodo di pagamento offerto")
        opt = _pick(opts, preference)
        data = {"paymentmethod": opt.value}
        data.update(self._hidden(BeautifulSoup(html, "lxml")))
        return self._post("payment_method", data), opt

    def save_payment_info(self, html: str) -> StepResponse:
        # Redirect-based methods (PayPal) have an empty info form; card forms would need data we never store.
        soup = BeautifulSoup(html, "lxml")
        if soup.find("input", attrs={"name": re.compile(r"CardNumber|CardCode|ExpireMonth", re.I)}):
            raise CheckoutError("il metodo di pagamento chiede i dati della carta: non supportato (scegli PayPal/bonifico/contrassegno)")
        return self._post("payment_info", self._hidden(soup))

    def summary(self, html: str) -> OrderSummary:
        soup = BeautifulSoup(html, "lxml")
        out = OrderSummary()
        for row in soup.select(".cart-item-row, tr.cart-item-row"):
            name = row.select_one(".product-name, .product a")
            qty = row.select_one(".qty, .product-quantity")
            sub = row.select_one(".product-subtotal, .subtotal")
            try:
                q = int(clean_text(qty.get_text())) if qty else 1
            except ValueError:
                q = 1
            out.items.append((clean_text(name.get_text()) if name else "?", q, parse_price(sub.get_text() if sub else None)[1]))
        for tr in soup.select(".cart-total tr, .total-info tr"):
            label = clean_text(tr.get_text(" ")).lower()
            val = tr.select_one(".cart-total-right, .value-summary, td:last-child")
            v = parse_price(val.get_text() if val else None)[1]
            if "parziale" in label or "subtotal" in label:
                out.subtotal = v
            elif "spedizione" in label or "shipping" in label:
                out.shipping = v
            elif label.startswith("totale") or label.startswith("total") or "order-total" in str(tr.get("class", "")):
                out.total = v
        sm = soup.select_one(".shipping-method .value, .selected-shipping-method")
        pm = soup.select_one(".payment-method .value, .selected-payment-method")
        out.shipping_method = clean_text(sm.get_text()) if sm else None
        out.payment_method = clean_text(pm.get_text()) if pm else None
        out.warnings = [clean_text(w.get_text()) for w in soup.select(".message-error li, .warning li")]
        return out

    def confirm(self, html: str) -> StepResponse:
        """PLACES THE ORDER. Only the runner calls this, after limits and human confirmation."""
        return self._post("confirm_order", self._hidden(BeautifulSoup(html, "lxml")))

    # ---- internals ---------------------------------------------------------
    def _post(self, step: str, data: dict) -> StepResponse:
        r = self.s.post(self.urls[step], data=data, ajax=True, referer=self.s.url(OPC_PATH))
        try:
            j = r.json()
        except ValueError:
            if "/login" in str(r.url).lower():
                raise CheckoutError("sessione scaduta durante il checkout")
            raise CheckoutError(f"risposta inattesa al passo {step} (HTTP {r.status_code})")
        resp = parse_step_response(j)
        self.last = resp
        if resp.error:
            raise CheckoutError(f"{step}: {resp.error}")
        return resp

    @staticmethod
    def _hidden(root) -> dict[str, str]:
        if root is None:
            return {}
        return {i.get("name"): i.get("value", "") for i in root.find_all("input", type="hidden") if i.get("name")}


def parse_step_response(j: dict) -> StepResponse:
    resp = StepResponse(raw=j)
    if j.get("error"):
        resp.error = clean_text(re.sub(r"<[^>]+>", " ", str(j.get("message") or "errore")))
        return resp
    if j.get("redirect"):
        resp.redirect = j["redirect"]
    upd = j.get("update_section") or {}
    resp.html = upd.get("html", "") or ""
    resp.section = j.get("goto_section") or (upd.get("name", "").replace("-", "_") if upd else None)
    return resp


def _pick(opts: list[Option], preference: str) -> Option:
    if preference:
        p = preference.lower()
        for o in opts:
            if p in o.label.lower() or p in o.value.lower():
                return o
        raise CheckoutError(f"opzione '{preference}' non offerta; disponibili: " + ", ".join(o.label for o in opts))
    return next((o for o in opts if o.selected), opts[0])
