"""Module: gemcardinfinitycollection.it (nopCommerce 3.x, theme DefaultClean). Studied 1 Oct 2026."""
from __future__ import annotations

from drop_monitor.account import AccountClient
from drop_monitor.order.runner import OrderRunner
from drop_monitor.sites import Procedure, SiteModule, register

AUTH = Procedure(
    name="auth",
    title="Login e registrazione",
    verified="form di registrazione, captcha, province, login: verificati sul sito il 1/10/2026 (senza inviare registrazioni)",
    steps=[
        {"step": "Apri il modulo", "method": "GET", "path": "/it/register?returnurl=/it/cart", "notes": "token anti-forgery hidden + cookie"},
        {"step": "Province", "method": "GET", "path": "/country/getstatesbycountryid?countryId=46&addSelectStateItem=true", "notes": "JSON id/name, 107 province"},
        {"step": "Captcha", "method": "GET", "path": "/DefaultCaptcha/Generate?t=<hash>", "notes": "GIF 200x70, domanda aritmetica; serve Referer; refresh con POST /DefaultCaptcha/Refresh"},
        {"step": "Invio", "method": "POST", "path": "/it/register?returnurl=/it/cart", "notes": "tutti i campi + consensi 'on' + CaptchaDeText/CaptchaInputText"},
        {"step": "Esito", "method": "-", "path": "/it/registerresult/1|2|3", "notes": "1 attivo, 2 approvazione negozio, 3 conferma e-mail; errori in .message-error li"},
        {"step": "Login", "method": "POST", "path": "/it/login?returnurl=/it/cart", "notes": "Email, Password, RememberMe + token; nessun captcha"},
    ],
    fields={
        "FirstName": "shipping.first_name", "LastName": "shipping.last_name", "Email": "account.email", "Company": "shipping.company",
        "StreetAddress": "shipping.address1 + address2", "ZipPostalCode": "shipping.zip", "City": "shipping.city",
        "CountryId": "shipping.country_id (46 = Italy)", "StateProvinceId": "shipping.province_id", "Phone": "shipping.phone",
        "Newsletter": "preferences.newsletter", "customer_attribute_1": "billing.fiscal_code", "customer_attribute_2": "billing.vat_number",
        "customer_attribute_6": "billing.sdi_code", "customer_attribute_7": "billing.pec", "customer_attribute_3": "preferences.referral",
        "Password/ConfirmPassword": ".env ORDER_PASSWORD[_NOME] (min 6)",
    },
    signals={"captcha": "CaptchaMvc: immagine con somma/sottrazione, letta da una persona nella finestra",
             "registrazione ok": "redirect a /registerresult/N", "login ok": "nessun redirect a /login"},
    limits=["una registrazione per e-mail", "captcha sempre richiesto", "esito reale (1/2/3) noto solo alla prima registrazione"],
)

CHECKOUT = Procedure(
    name="checkout",
    title="Carrello e acquisto",
    verified="add-to-cart, carrello e redirect al login verificati in anonimo il 1/10/2026; sezioni OPC dal JS standard nop 3.x (markup da confermare con --probe)",
    steps=[
        {"step": "Aggiungi", "method": "POST", "path": "/it/addproducttocart/catalog/{id}/1/{qty}", "notes": "AJAX JSON {success, message}"},
        {"step": "Carrello", "method": "GET", "path": "/it/cart", "notes": "righe removefromcart/itemquantity, subtotale, checkbox termini, token"},
        {"step": "Vai al checkout", "method": "POST", "path": "/it/cart", "notes": "checkout=checkout + termsofservice=true -> /it/onepagecheckout (login obbligatorio, no ospite)"},
        {"step": "Indirizzo", "method": "POST", "path": "/it/checkout/OpcSaveBilling/", "notes": "billing_address_id esistente o BillingNewAddress.*; ShipToSameAddress"},
        {"step": "Spedizione", "method": "POST", "path": "/it/checkout/OpcSaveShippingMethod/", "notes": "shippingoption = 'Nome___Sistema' scelto per nome"},
        {"step": "Pagamento", "method": "POST", "path": "/it/checkout/OpcSavePaymentMethod/", "notes": "paymentmethod = Payments.X; carte nel sito rifiutate"},
        {"step": "Dati pagamento", "method": "POST", "path": "/it/checkout/OpcSavePaymentInfo/", "notes": "vuoto per PayPal/bonifico"},
        {"step": "Riepilogo", "method": "-", "path": "sezione confirm-order", "notes": "righe, subtotale, spedizione, TOTALE letto dal sito"},
        {"step": "Conferma", "method": "POST", "path": "/it/checkout/OpcConfirmOrder/", "notes": "solo dopo limite + conferma umana; redirect /checkout/completed o PayPal"},
    ],
    fields={"indirizzo": "profilo shipping.*", "pagamento": "task.payment_method o preferences.payment_method", "spedizione": "task.shipping_method"},
    signals={"disponibile (lista)": "overlay 'Esaurito' assente nella categoria", "disponibile (pagina)": "blocco back-in-stock assente",
             "ordine piazzato": "redirect contiene /completed", "pagamento esterno": "redirect a PayPal"},
    limits=["niente checkout ospite", "metodi di pagamento reali noti solo da loggati", "quantità max per ordine sconosciuta"],
)

MONITOR = Procedure(
    name="monitor",
    title="Monitoraggio",
    verified="categoria, ricerca, pagina prodotto, RSS, sitemap, robots.txt: verificati il 1/10/2026",
    steps=[
        {"step": "Categoria", "method": "GET", "path": "/it/pokemon-30%C2%BA-anniversario?pagenumber=N", "notes": "6 prodotti/pagina, overlay Esaurito, prezzo"},
        {"step": "Pagina prodotto", "method": "GET", "path": "/it/<slug>", "notes": "segnale certo: back-in-stock presente = esaurito"},
        {"step": "Nuovi prodotti", "method": "GET", "path": "/newproducts/rss", "notes": "ultimi 10, senza prezzo/stock"},
    ],
    signals={"esaurito": "div overlay 'Esaurito' / div.back-in-stock-subscription", "prezzo": "span.actual-price / #price-value-ID"},
    limits=["SmartSearch in Disallow per Googlebot e senza stock: non usata", "pagesize ignorato"],
)


def _auth_factory(base_url: str, user_agent: str, timeout: float = 20) -> AccountClient:
    return AccountClient(base_url, user_agent, timeout)


def _checkout_factory(base_url: str, user_agent: str, profiles, store, **kw) -> OrderRunner:
    return OrderRunner(base_url, user_agent, profiles, store, **kw)


MODULE = register(SiteModule(
    key="gemcard",
    domains=("gemcardinfinitycollection.it", "www.gemcardinfinitycollection.it"),
    platform="nopCommerce 3.x",
    title="Gemcard Infinity Collection",
    procedures={"auth": AUTH, "checkout": CHECKOUT, "monitor": MONITOR},
    auth_factory=_auth_factory,
    checkout_factory=_checkout_factory,
    parser_hint="category",
    notes=["Tema DefaultClean personalizzato", "Cookie: Nop.customer, ASP.NET_SessionId, __RequestVerificationToken", "Nessun WAF rilevato"],
))
