from pathlib import Path

import pytest

from drop_monitor.account import (
    AccountError,
    build_registration_payload,
    classify_registration_response,
    parse_registration_form,
)

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.gemcardinfinitycollection.it"
REG_URL = f"{BASE}/it/register?returnurl=%2fit%2fcart"

PROFILE = {
    "account": {"email": "mario.rossi@example.com", "password": ""},
    "shipping": {"first_name": "Mario", "last_name": "Rossi", "company": "", "address1": "Via Roma 1", "address2": "int. 3",
                 "zip": "20100", "city": "Milano", "province": "Milano", "province_id": 130, "country": "Italy", "country_id": 46, "phone": "3331234567"},
    "billing": {"fiscal_code": "RSSMRA80A01F205X", "vat_number": "", "sdi_code": "", "pec": ""},
    "preferences": {"newsletter": False, "referral": "5"},
}


@pytest.fixture
def form(fixture_text):
    return parse_registration_form(fixture_text("nop_register_form.html"), REG_URL)


def test_parse_registration_form(form):
    assert form.action == REG_URL
    assert len(form.token) > 40
    assert form.captcha_token and len(form.captcha_token) == 32
    assert form.captcha_image_url == f"{BASE}/DefaultCaptcha/Generate?t={form.captcha_token}"
    assert set(form.required_names) >= {"FirstName", "LastName", "Email", "StreetAddress", "ZipPostalCode", "City", "CountryId", "StateProvinceId", "Phone", "Password", "ConfirmPassword"}
    assert form.fields["Company"].required is False
    assert form.fields["customer_attribute_1"].label == "Codice Fiscale:"
    assert form.fields["customer_attribute_7"].label == "Indirizzo PEC:"
    assert ("46", "Italy") in form.countries
    assert form.consents == ["accept-privacy-policy", "accept-privacy-termini", "accept-privacy-registrazione"]
    assert ("5", "Passaparola") in form.referral_options
    assert form.fields["Password"].rules["length-min"] == "6"
    assert "CaptchaDeText" not in form.fields
    d = form.to_dict()
    assert d["has_captcha"] and len(d["fields"]) == len(form.fields)


def test_parse_registration_form_missing():
    with pytest.raises(AccountError):
        parse_registration_form("<html><body><p>Registrazione chiusa</p></body></html>", REG_URL)


def test_build_payload_maps_profile(form):
    p = build_registration_payload(form, PROFILE, "segreta1", "ab12cd")
    assert p["__RequestVerificationToken"] == form.token
    assert p["FirstName"] == "Mario" and p["LastName"] == "Rossi" and p["Email"] == "mario.rossi@example.com"
    assert p["StreetAddress"] == "Via Roma 1 int. 3"
    assert p["CountryId"] == "46" and p["StateProvinceId"] == "130"
    assert p["Password"] == p["ConfirmPassword"] == "segreta1"
    assert p["Newsletter"] == "false"
    assert p["customer_attribute_1"] == "RSSMRA80A01F205X" and p["customer_attribute_3"] == "5"
    assert p["CaptchaDeText"] == form.captcha_token and p["CaptchaInputText"] == "ab12cd"
    assert all(p[c] == "on" for c in form.consents)
    assert p["register-button"] == "Registrati"
    p2 = build_registration_payload(form, PROFILE, "segreta1", "ab12cd", newsletter=True)
    assert p2["Newsletter"] == "true"


def test_build_payload_reports_missing(form):
    prof = {**PROFILE, "shipping": {**PROFILE["shipping"], "city": "", "province_id": 0}}
    with pytest.raises(AccountError) as ei:
        build_registration_payload(form, prof, "abc", "")
    msg = str(ei.value)
    assert "Città" in msg and "Stato/provincia" in msg and "Password (min 6" in msg and "captcha" in msg.lower()


def test_classify_results(fixture_text):
    r = classify_registration_response("<html><div class='result'>Registrazione completata</div></html>", f"{BASE}/it/registerresult/1?returnurl=%2fit%2fcart")
    assert r.ok and r.result_id == "1" and "completata" in r.message.lower()
    r = classify_registration_response("<html></html>", f"{BASE}/it/registerresult/3")
    assert r.ok and "e-mail" in r.message
    r = classify_registration_response("<html></html>", f"{BASE}/it/cart", return_url="/it/cart")
    assert r.ok
    import re

    html = re.sub(r'<p class="Error">\s*</p>', '<p class="Error">Captcha errato</p>', fixture_text("nop_register_form.html"))
    html = html.replace("<body>", '<body><div class="message-error validation-summary-errors"><ul><li>L\'indirizzo e-mail è già in uso</li></ul></div>')
    r = classify_registration_response(html, REG_URL, return_url="/it/cart")
    assert not r.ok and "già in uso" in r.errors[0] and "Captcha errato" in r.errors
