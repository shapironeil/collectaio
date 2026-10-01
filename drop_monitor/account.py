"""Customer account on a nopCommerce shop: registration form study, payload building, submission.

Verified on gemcardinfinitycollection.it (Oct 2026), see docs/site-analysis.md:

* GET  /it/register?returnurl=%2fit%2fcart  -> form with __RequestVerificationToken (hidden + cookie)
* fields: FirstName, LastName, Email, Company, StreetAddress, ZipPostalCode, City, CountryId (46 = Italy),
  StateProvinceId (loaded via GET /country/getstatesbycountryid?countryId=N&addSelectStateItem=true),
  Phone, Newsletter, Password/ConfirmPassword (min 6), customer_attribute_1 Codice Fiscale,
  _2 Partita IVA, _6 Codice SDI, _7 PEC, _3 "Come ci hai conosciuto?" (radio 1..8)
* an image captcha (CaptchaMvc): <img src="/DefaultCaptcha/Generate?t=HASH">, hidden CaptchaDeText=HASH,
  answer in CaptchaInputText. A human must read it: this module never tries to solve it.
* three consent checkboxes (accept-privacy-policy / -termini / -registrazione) enforced client-side only.
* POST to the same URL. Success redirects to /it/registerresult/{1|2|3} (standard / admin approval /
  e-mail validation) or straight to returnurl; failure re-renders the form with validation errors.
* /it/login has no captcha (Email, Password, RememberMe).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from drop_monitor.parsers.common import clean_text

log = logging.getLogger(__name__)

REGISTER_PATH = "/it/register"
LOGIN_PATH = "/it/login"
STATES_PATH = "/country/getstatesbycountryid"
CUSTOMER_ATTRIBUTES = {  # name on the site -> profile key
    "customer_attribute_1": ("billing", "fiscal_code"),
    "customer_attribute_2": ("billing", "vat_number"),
    "customer_attribute_6": ("billing", "sdi_code"),
    "customer_attribute_7": ("billing", "pec"),
}
REGISTER_RESULT_MESSAGES = {
    "1": "Registrazione completata: l'account e' attivo.",
    "2": "Registrazione ricevuta: l'account deve essere approvato dal negozio.",
    "3": "Registrazione ricevuta: conferma l'indirizzo e-mail dal messaggio che ti e' stato inviato.",
    "4": "Registrazione disabilitata dal negozio.",
}


class AccountError(Exception):
    pass


@dataclass
class FieldSpec:
    name: str
    type: str
    required: bool = False
    label: str | None = None
    options: list[tuple[str, str]] = field(default_factory=list)
    rules: dict[str, str] = field(default_factory=dict)


@dataclass
class RegistrationForm:
    action: str
    token: str
    fields: dict[str, FieldSpec]
    captcha_token: str | None = None
    captcha_image_url: str | None = None
    consents: list[str] = field(default_factory=list)
    countries: list[tuple[str, str]] = field(default_factory=list)
    referral_options: list[tuple[str, str]] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    @property
    def required_names(self) -> list[str]:
        return [n for n, f in self.fields.items() if f.required]

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "has_captcha": bool(self.captcha_token),
            "captcha_image_url": self.captcha_image_url,
            "consents": self.consents,
            "countries": self.countries,
            "referral_options": self.referral_options,
            "sections": self.sections,
            "fields": [
                {"name": f.name, "type": f.type, "required": f.required, "label": f.label, "rules": f.rules}
                for f in self.fields.values()
            ],
        }


@dataclass
class RegistrationResult:
    ok: bool
    message: str
    errors: list[str] = field(default_factory=list)
    final_url: str = ""
    result_id: str | None = None


def parse_registration_form(html: str, base_url: str) -> RegistrationForm:
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", action=re.compile(r"/register", re.I))
    if form is None:
        raise AccountError("registration form not found (page layout changed or registration disabled)")
    token_el = form.find("input", attrs={"name": "__RequestVerificationToken"})
    if token_el is None or not token_el.get("value"):
        raise AccountError("anti-forgery token not found in registration form")

    fields: dict[str, FieldSpec] = {}
    consents: list[str] = []
    referral: list[tuple[str, str]] = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or name in ("__RequestVerificationToken", "register-button"):
            continue
        typ = (el.get("type") or el.name).lower()
        if typ in ("submit", "button"):
            continue
        if name.startswith("accept-"):
            consents.append(name)
            continue
        if typ == "radio":
            label = soup.find("label", attrs={"for": el.get("id")})
            referral.append((el.get("value", ""), clean_text(label.get_text()) if label else el.get("value", "")))
            if name not in fields:
                fields[name] = FieldSpec(name=name, type="radio", label=_block_label(el))
            continue
        if name in fields:  # hidden twin of a checkbox (Newsletter=false)
            continue
        label_el = soup.find("label", attrs={"for": el.get("id")}) if el.get("id") else None
        label = clean_text(label_el.get_text()) if label_el else _block_label(el)
        rules = {k[9:]: v for k, v in el.attrs.items() if k.startswith("data-val-") and k != "data-val"}
        spec = FieldSpec(name=name, type=typ, required="required" in rules, label=label or None, rules=rules)
        if el.name == "select":
            spec.options = [(o.get("value", ""), clean_text(o.get_text())) for o in el.find_all("option")]
        fields[name] = spec
    if referral:
        fields["customer_attribute_3"].options = referral

    captcha_el = form.find("input", attrs={"name": "CaptchaDeText"})
    captcha_token = captcha_el.get("value") if captcha_el else None
    img = form.find("img", id="CaptchaImage")
    captcha_url = urljoin(base_url, img["src"]) if img and img.get("src") else None
    fields.pop("CaptchaDeText", None)
    countries = fields["CountryId"].options if "CountryId" in fields else []
    sections = [clean_text(t.get_text()) for t in form.select(".fieldset .title strong, fieldset .title strong")]
    return RegistrationForm(
        action=urljoin(base_url, form.get("action") or REGISTER_PATH),
        token=token_el["value"],
        fields=fields,
        captcha_token=captcha_token,
        captcha_image_url=captcha_url,
        consents=consents,
        countries=countries,
        referral_options=referral,
        sections=sections,
    )


def _block_label(el) -> str | None:
    block = el.find_parent("div", class_=re.compile("inputs|form-fields"))
    lab = block.find("label") if block else None
    return clean_text(lab.get_text()) if lab else None


def build_registration_payload(form: RegistrationForm, profile: dict, password: str, captcha_answer: str | None,
                               newsletter: bool | None = None) -> dict[str, str]:
    """Map the buyer profile onto the site's field names. Raises AccountError listing what is missing."""
    sh, bl, pref, acc = profile.get("shipping", {}), profile.get("billing", {}), profile.get("preferences", {}), profile.get("account", {})
    values: dict[str, str] = {
        "__RequestVerificationToken": form.token,
        "FirstName": sh.get("first_name", ""),
        "LastName": sh.get("last_name", ""),
        "Email": acc.get("email", ""),
        "Company": sh.get("company", ""),
        "StreetAddress": " ".join(x for x in (sh.get("address1", ""), sh.get("address2", "")) if x).strip(),
        "ZipPostalCode": str(sh.get("zip", "")),
        "City": sh.get("city", ""),
        "CountryId": str(sh.get("country_id") or 0),
        "StateProvinceId": str(sh.get("province_id") or 0),
        "Phone": str(sh.get("phone", "")),
        "Newsletter": "true" if (pref.get("newsletter") if newsletter is None else newsletter) else "false",
        "Password": password or "",
        "ConfirmPassword": password or "",
    }
    for site_name, (section, key) in CUSTOMER_ATTRIBUTES.items():
        if site_name in form.fields:
            values[site_name] = str(profile.get(section, {}).get(key, "") or "")
    if "customer_attribute_3" in form.fields and pref.get("referral"):
        values["customer_attribute_3"] = str(pref["referral"])
    if form.captcha_token:
        values["CaptchaDeText"] = form.captcha_token
        values["CaptchaInputText"] = (captcha_answer or "").strip()
    for name in form.consents:
        values[name] = "on"
    values["register-button"] = "Registrati"

    missing = []
    for name in form.required_names:
        if name not in values or values[name] in ("", "0"):
            missing.append(form.fields[name].label or name)
    pw_min = int(form.fields.get("Password", FieldSpec("Password", "password")).rules.get("length-min", 6) or 6)
    if len(password or "") < pw_min:
        missing.append(f"Password (min {pw_min} caratteri)")
    if form.captcha_token and not values.get("CaptchaInputText"):
        missing.append("Risposta captcha")
    if "@" not in values["Email"]:
        missing.append("E-mail valida")
    if missing:
        raise AccountError("Dati mancanti o non validi: " + ", ".join(dict.fromkeys(missing)))
    return values


def classify_registration_response(html: str, final_url: str, return_url: str | None = None) -> RegistrationResult:
    m = re.search(r"/registerresult/(\d+)", final_url)
    if m:
        rid = m.group(1)
        ok = rid in ("1", "2", "3")
        msg = REGISTER_RESULT_MESSAGES.get(rid, f"Esito registrazione: codice {rid}")
        soup = BeautifulSoup(html, "lxml")
        res = soup.select_one(".registration-result-page .result, .result")
        if res:
            msg = f"{msg} ({clean_text(res.get_text())})"
        return RegistrationResult(ok=ok, message=msg, final_url=final_url, result_id=rid)
    if return_url and urlsplit(final_url).path.rstrip("/") == urlsplit(urljoin(final_url, return_url)).path.rstrip("/"):
        return RegistrationResult(ok=True, message="Registrazione completata: sei stato reindirizzato alla pagina richiesta.", final_url=final_url)
    soup = BeautifulSoup(html, "lxml")
    errors = [clean_text(li.get_text()) for li in soup.select(".message-error li, .validation-summary-errors li")]
    errors += [clean_text(s.get_text()) for s in soup.select("span.field-validation-error") if clean_text(s.get_text())]
    errors += [clean_text(p.get_text()) for p in soup.select(".captcha-box p.Error") if clean_text(p.get_text())]
    errors = list(dict.fromkeys(e for e in errors if e))
    if soup.find("form", action=re.compile(r"/register", re.I)):
        return RegistrationResult(ok=False, message="Il sito ha rifiutato la registrazione.", errors=errors or ["Errore non specificato (captcha errato?)"], final_url=final_url)
    return RegistrationResult(ok=False, message="Risposta inattesa dal sito.", errors=errors, final_url=final_url)


class AccountClient:
    """One browser-like session (cookies kept) for registration/login on the shop."""

    def __init__(self, base_url: str, user_agent: str, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "it-IT,it;q=0.9,en;q=0.5"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )
        self.form: RegistrationForm | None = None
        self.return_url = "/it/cart"

    def close(self) -> None:
        self._client.close()

    def start_registration(self, return_url: str = "/it/cart") -> RegistrationForm:
        self.return_url = return_url
        url = f"{self.base_url}{REGISTER_PATH}?returnurl={httpx.URL(return_url).path}"
        r = self._client.get(url)
        r.raise_for_status()
        self.form = parse_registration_form(r.text, str(r.url))
        return self.form

    def captcha_image(self) -> tuple[bytes, str]:
        if not self.form or not self.form.captcha_image_url:
            raise AccountError("no captcha on the current form")
        r = self._client.get(self.form.captcha_image_url)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/gif")

    def refresh_captcha(self) -> None:
        """Ask the site for a new image for the same token (what the page's refresh link does)."""
        if not self.form or not self.form.captcha_token:
            raise AccountError("no captcha on the current form")
        r = self._client.post(f"{self.base_url}/DefaultCaptcha/Refresh", data={"t": self.form.captcha_token, "__m__": "0"})
        r.raise_for_status()
        new_token = r.text.strip().strip('"')
        if re.fullmatch(r"[0-9a-f]{32}", new_token):  # some versions answer with the new token
            self.form.captcha_token = new_token
            self.form.captcha_image_url = f"{self.base_url}/DefaultCaptcha/Generate?t={new_token}"

    def states(self, country_id: int | str) -> list[dict]:
        r = self._client.get(f"{self.base_url}{STATES_PATH}", params={"countryId": country_id, "addSelectStateItem": "true"})
        r.raise_for_status()
        return [s for s in r.json() if int(s.get("id", 0)) != 0]

    def submit_registration(self, payload: dict[str, str]) -> RegistrationResult:
        if not self.form:
            raise AccountError("call start_registration() first")
        r = self._client.post(self.form.action, data=payload, headers={"Referer": self.form.action})
        result = classify_registration_response(r.text, str(r.url), self.return_url)
        log.info("registration submitted: ok=%s %s %s", result.ok, result.message, result.errors)
        return result

    def login(self, email: str, password: str, return_url: str = "/it/cart") -> bool:
        url = f"{self.base_url}{LOGIN_PATH}?returnurl={httpx.URL(return_url).path}"
        r = self._client.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        tok = soup.find("input", attrs={"name": "__RequestVerificationToken"})
        data = {"Email": email, "Password": password, "RememberMe": "false"}
        if tok:
            data["__RequestVerificationToken"] = tok["value"]
        r = self._client.post(url, data=data, headers={"Referer": url})
        return "/login" not in str(r.url).lower()
