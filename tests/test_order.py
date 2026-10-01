"""Order engine tests with a fake shop session (no network, no real orders)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from drop_monitor.order.cart import Cart, CartError, parse_cart
from drop_monitor.order.checkout import CheckoutError, OnePageCheckout, parse_step_response
from drop_monitor.order.models import Task
from drop_monitor.order.runner import OrderRunner
from drop_monitor.store import Store

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.gemcardinfinitycollection.it"

PROFILE = {
    "name": "default",
    "account": {"email": "mario@example.com", "password": "segreta1"},
    "shipping": {"first_name": "Mario", "last_name": "Rossi", "company": "", "address1": "Via Roma 1", "address2": "", "zip": "20100",
                 "city": "Milano", "province_id": 130, "country_id": 46, "phone": "333"},
    "billing": {}, "preferences": {"max_total_eur": 150, "confirm_on_telegram": True, "shipping_method": "", "payment_method": ""},
}

OPC_PAGE = """<html><body>
<form id="co-billing-form"><select id="billing-address-select"><option value="77">Mario Rossi, Via Roma 1, 20100 Milano</option><option value="">Nuovo indirizzo</option></select>
<input type="hidden" name="__RequestVerificationToken" value="T1"></form>
<script>Billing.init('#co-billing-form', '/it/checkout/OpcSaveBilling/', false);ShippingMethod.init('#co-shipping-method-form', '/it/checkout/OpcSaveShippingMethod/');
PaymentMethod.init('#co-payment-method-form', '/it/checkout/OpcSavePaymentMethod/');PaymentInfo.init('#co-payment-info-form', '/it/checkout/OpcSavePaymentInfo/');ConfirmOrder.init('/it/checkout/OpcConfirmOrder/', '/it/cart');</script>
</body></html>"""
SHIP_HTML = """<ul><li><input id="shippingoption_0" type="radio" name="shippingoption" value="Corriere Espresso___Shipping.FixedRate" checked><label for="shippingoption_0">Corriere Espresso (€7,90)</label></li>
<li><input id="shippingoption_1" type="radio" name="shippingoption" value="Ritiro___Pickup.PickupInStore"><label for="shippingoption_1">Ritiro in negozio (€0,00)</label></li></ul>"""
PAY_HTML = """<ul><li><div class="method-name"><input id="paymentmethod_0" type="radio" name="paymentmethod" value="Payments.PayPalStandard" checked><label for="paymentmethod_0">PayPal</label></div></li>
<li><div class="method-name"><input id="paymentmethod_1" type="radio" name="paymentmethod" value="Payments.CheckMoneyOrder"><label for="paymentmethod_1">Bonifico bancario</label></div></li></ul>"""
INFO_HTML = "<div class='info'>Verrai reindirizzato a PayPal</div>"
CONFIRM_HTML = """<div class="order-summary-content"><table><tr class="cart-item-row"><td class="product"><a class="product-name">Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA</a></td><td class="qty">1</td><td class="subtotal"><span class="product-subtotal">€129,89</span></td></tr></table>
<table class="cart-total"><tr><td>Totale parziale:</td><td class="cart-total-right">€129,89</td></tr><tr><td>Spedizione:</td><td class="cart-total-right">€7,90</td></tr><tr class="order-total"><td>Totale:</td><td class="cart-total-right"><strong>€137,79</strong></td></tr></table></div>"""


class FakeSession:
    """Routes (method, path) -> list of responses; records the posted data."""

    def __init__(self, routes, profile=PROFILE, logged_in=True):
        self.routes = routes
        self.profile = profile
        self.name = profile["name"]
        self.base_url = BASE
        self.posts: list[tuple[str, dict | None]] = []
        self.logged_in = logged_in
        self.closed = False

    def url(self, p):
        return p if p.startswith("http") else BASE + p

    def _resp(self, method, path, data=None, content=None):
        key = (method, path.split("?")[0])
        route = self.routes.get(key)
        if route is None:
            return _r(404, "<html>Pagina non trovata</html>", url=self.url(path))
        if callable(route):
            route = route(data, content)
        if isinstance(route, tuple):
            body, url = route
        else:
            body, url = route, self.url(path)
        if isinstance(body, dict):
            return _r(200, json.dumps(body), url=url, ctype="application/json")
        return _r(200, body, url=url)

    def get(self, path, **kw):
        return self._resp("GET", path)

    def post(self, path, data=None, content=None, **kw):
        self.posts.append((path.split("?")[0], data if data is not None else content))
        return self._resp("POST", path, data, content)

    def ensure_logged_in(self):
        if not self.logged_in:
            from drop_monitor.order.session import LoginError

            raise LoginError("credenziali non valide")

    def close(self):
        self.closed = True


def _r(status, text, url, ctype="text/html"):
    return httpx.Response(status, text=text, headers={"content-type": ctype}, request=httpx.Request("GET", url))


CART_HTML = (FIX / "nop_cart_one_item.html").read_text(encoding="utf-8")
EMPTY_CART = CART_HTML.replace('name="removefromcart"', 'name="gone"')


def routes_ok(total_html=CONFIRM_HTML, confirm_redirect="/it/checkout/completed/"):
    cart_state = {"items": 0}

    def cart_view(data=None, content=None):
        return CART_HTML if cart_state["items"] else EMPTY_CART

    def cart_post(data, content):
        if content and "removefromcart" in content:
            cart_state["items"] = 0
            return EMPTY_CART
        if data and data.get("checkout"):
            return (OPC_PAGE, BASE + "/it/onepagecheckout")
        return CART_HTML

    def add(data, content):
        cart_state["items"] = 1
        return {"success": True, "message": "<div>Il prodotto è stato aggiunto al tuo carrello</div>"}

    return {
        ("GET", "/it/cart"): cart_view,
        ("POST", "/it/cart"): cart_post,
        ("POST", "/it/addproducttocart/catalog/3765/1/1"): add,
        ("GET", "/it/onepagecheckout"): OPC_PAGE,
        ("POST", "/it/checkout/OpcSaveBilling/"): {"update_section": {"name": "shipping-method", "html": SHIP_HTML}, "goto_section": "shipping_method"},
        ("POST", "/it/checkout/OpcSaveShippingMethod/"): {"update_section": {"name": "payment-method", "html": PAY_HTML}, "goto_section": "payment_method"},
        ("POST", "/it/checkout/OpcSavePaymentMethod/"): {"update_section": {"name": "payment-info", "html": INFO_HTML}, "goto_section": "payment_info"},
        ("POST", "/it/checkout/OpcSavePaymentInfo/"): {"update_section": {"name": "confirm-order", "html": total_html}, "goto_section": "confirm_order"},
        ("POST", "/it/checkout/OpcConfirmOrder/"): {"redirect": confirm_redirect},
    }


class FakeProfiles:
    def __init__(self, profile=PROFILE):
        self.profile = profile

    def load(self, name):
        return {**self.profile, "name": name}


class YesConfirmer:
    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []

    def ask(self, text, timeout):
        self.asked.append(text)
        return self.answer


def _runner(session, confirmer=None, notify=None):
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=confirmer or YesConfirmer(), notify=notify)
    r._make_session = lambda *a, **k: session  # not used by default path; we monkeypatch below
    return r


@pytest.fixture
def patch_session(monkeypatch):
    def _apply(session):
        monkeypatch.setattr("drop_monitor.order.runner.ShopSession", lambda *a, **k: session)
        return session
    return _apply


# ---------------------------------------------------------------- cart
def test_parse_real_cart_page():
    v = parse_cart(CART_HTML)
    assert len(v.items) == 1 and v.items[0].item_id == "1083576" and v.items[0].quantity == 1
    assert v.items[0].unit_price == 24.69 and v.subtotal == 24.69
    assert v.has_terms_checkbox and v.token and len(v.token) > 20
    assert v.total is None  # "Calcolato in fase di acquisto"


def test_cart_add_and_clear():
    s = FakeSession(routes_ok())
    cart = Cart(s)
    assert "aggiunto" in cart.add(3765, 1)
    assert cart.view().quantity == 1
    assert cart.clear().items == []
    s.routes[("POST", "/it/addproducttocart/catalog/3765/1/1")] = {"success": False, "message": "<p>Esaurito</p>"}
    with pytest.raises(CartError, match="Esaurito"):
        cart.add(3765, 1)


# ------------------------------------------------------------ checkout
def test_opc_parses_urls_addresses_and_options():
    s = FakeSession(routes_ok())
    opc = OnePageCheckout(s)
    opc.start()
    assert opc.urls["confirm_order"] == "/it/checkout/OpcConfirmOrder/"
    assert opc.choose_address(PROFILE) == "77"
    assert opc.choose_address({"shipping": {"address1": "Via Verdi 9", "zip": "00100"}}) is None
    ship = opc.options(SHIP_HTML, "shippingoption")
    assert [o.price for o in ship] == [7.9, 0.0] and ship[0].selected
    step, opt = opc.save_shipping_method(SHIP_HTML, "ritiro")
    assert opt.value.startswith("Ritiro") and step.section == "payment_method"
    with pytest.raises(CheckoutError, match="non offerta"):
        opc.save_shipping_method(SHIP_HTML, "drone")
    step, opt = opc.save_payment_method(PAY_HTML, "")
    assert opt.value == "Payments.PayPalStandard"
    summ = opc.summary(CONFIRM_HTML)
    assert summ.total == 137.79 and summ.subtotal == 129.89 and summ.shipping == 7.9 and summ.items[0][1] == 1
    assert parse_step_response({"error": 1, "message": "<b>Indirizzo</b> mancante"}).error == "Indirizzo mancante"
    assert parse_step_response({"redirect": "/x"}).redirect == "/x"


def test_card_payment_info_is_refused():
    s = FakeSession(routes_ok())
    opc = OnePageCheckout(s)
    with pytest.raises(CheckoutError, match="carta"):
        opc.save_payment_info('<form><input name="CardNumber"></form>')


# -------------------------------------------------------------- runner
TASK = Task(name="minitin", product="Pokemon 30 Anniversario Mini Tin Case ITA", profiles=["default"], quantity=1, mode="auto_checkout", max_total_eur=150)


def test_runner_dry_run_stops_before_confirm(patch_session):
    s = patch_session(FakeSession(routes_ok()))
    notes = []
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=YesConfirmer(), notify=notes.append)
    (res,) = r.run_task(TASK, "3765", "Mini Tin Case", dry_run=True)
    assert res.status == "dry_run" and res.total_eur == 137.79
    assert not any(p[0] == "/it/checkout/OpcConfirmOrder/" for p in s.posts)
    assert any("dry-run" in n for n in notes)
    assert [st.step for st in res.steps] == ["login", "carrello", "indirizzo", "spedizione", "pagamento", "riepilogo"]
    assert s.closed and r.store.recent_orders()[0]["status"] == "dry_run"


def test_runner_live_places_order_after_confirmation(patch_session):
    s = patch_session(FakeSession(routes_ok(confirm_redirect="https://www.paypal.com/checkout?token=abc")))
    conf = YesConfirmer(True)
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=conf)
    (res,) = r.run_task(TASK, "3765", "Mini Tin Case", dry_run=False)
    assert res.status == "pending_payment" and res.order_url.startswith("https://www.paypal.com")
    assert len(conf.asked) == 1 and "137.79" in conf.asked[0]
    billing = next(d for p, d in s.posts if p == "/it/checkout/OpcSaveBilling/")
    assert billing["billing_address_id"] == "77" and billing["ShipToSameAddress"] == "true"
    assert s.posts[-1][0] == "/it/checkout/OpcConfirmOrder/"


def test_runner_respects_limit_and_cancellation(patch_session):
    s = patch_session(FakeSession(routes_ok()))
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=YesConfirmer(True))
    (res,) = r.run_task(Task(name="t", product=TASK.product, max_total_eur=100), "3765", "x")
    assert res.status == "over_limit" and not any(p[0].endswith("OpcConfirmOrder/") for p in s.posts)
    s2 = patch_session(FakeSession(routes_ok()))
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=YesConfirmer(False))
    (res,) = r.run_task(TASK, "3765", "x")
    assert res.status == "cancelled" and not any(p[0].endswith("OpcConfirmOrder/") for p in s2.posts)
    s3 = patch_session(FakeSession(routes_ok()))
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=YesConfirmer(None))
    (res,) = r.run_task(TASK, "3765", "x")
    assert res.status == "timeout"


def test_runner_reports_login_and_cart_failures(patch_session):
    patch_session(FakeSession(routes_ok(), logged_in=False))
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"))
    (res,) = r.run_task(TASK, "3765", "x", dry_run=True)
    assert res.status == "failed" and "credenziali" in res.message
    routes = routes_ok()
    routes[("POST", "/it/addproducttocart/catalog/3765/1/1")] = {"success": False, "message": "Esaurito"}
    patch_session(FakeSession(routes))
    (res,) = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:")).run_task(TASK, "3765", "x", dry_run=True)
    assert res.status == "failed" and "Esaurito" in res.message


def test_runner_multiple_profiles_and_no_duplicate_runs(patch_session):
    patch_session(FakeSession(routes_ok()))
    task = Task(name="x2", product=TASK.product, profiles=["default", "nonna"], mode="auto_checkout")
    r = OrderRunner(BASE, "ua", FakeProfiles(), Store(":memory:"), confirmer=YesConfirmer())
    results = r.run_task(task, "3765", "x", dry_run=True)
    assert [x.profile for x in results] == ["default", "nonna"] and all(x.status == "dry_run" for x in results)
    r._running.add("x2:3765")
    assert r.run_task(task, "3765", "x", dry_run=True) == []


def test_config_tasks_validation():
    from drop_monitor.config import ConfigError, parse_config

    base = {"site": {"url": "https://shop.example/c"}, "products": ["Mini Tin Case"], "telegram": {"enabled": False}}
    cfg = parse_config({**base, "tasks": [{"name": "t", "product": "Mini Tin Case", "profiles": "default", "mode": "auto_checkout"}]})
    assert cfg.tasks[0].profiles == ["default"] and cfg.tasks[0].max_total_eur == 150
    with pytest.raises(ConfigError, match="does not match"):
        parse_config({**base, "tasks": [{"name": "t", "product": "Altro"}]})
    with pytest.raises(ConfigError, match="mode"):
        parse_config({**base, "tasks": [{"name": "t", "product": "Mini Tin Case", "mode": "yolo"}]})
