from drop_monitor.models import Availability, FetchResult
from drop_monitor.parsers import parse
from drop_monitor.parsers.common import parse_price

BASE = "https://www.gemcardinfinitycollection.it"
CAT2 = f"{BASE}/it/pokemon-30%C2%BA-anniversario?pagenumber=2"


def _html(text, url, hint="auto", ctype="text/html; charset=utf-8"):
    return parse(FetchResult(url=url, status=200, text=text, content_type=ctype), hint=hint)


def test_category_grid_all_out_of_stock(fixture_text):
    r = _html(fixture_text("nop_category_page2_all_oos.html"), CAT2)
    assert r.kind == "category"
    assert len(r.products) == 6
    by_id = {p.product_id: p for p in r.products}
    tin = by_id["3765"]
    assert tin.title == "Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA"
    assert tin.url == f"{BASE}/it/pokemon-30-anniversario-mini-tin-case-sealed-ita"
    assert tin.price == "€129,89" and tin.price_value == 129.89
    assert tin.availability == Availability.UNAVAILABLE  # "Esaurito" overlay
    assert tin.add_to_cart_url == f"{BASE}/it/addproducttocart/catalog/3765/1/1"
    assert all(p.availability == Availability.UNAVAILABLE for p in r.products)


def test_category_grid_overlay_removed_means_available(fixture_text):
    r = _html(fixture_text("nop_category_page2_minitin_available.html"), CAT2)
    by_id = {p.product_id: p for p in r.products}
    assert by_id["3765"].availability == Availability.AVAILABLE
    assert by_id["3765"].price_value == 134.89
    assert by_id["3766"].availability == Availability.UNAVAILABLE


def test_category_pagination(fixture_text):
    r = _html(fixture_text("nop_category_page2_all_oos.html"), CAT2)
    assert r.page_number == 2
    assert r.total_pages == 4
    assert len(r.next_pages) == 3
    assert all("pagenumber=" in u or u.endswith("anniversario") for u in r.next_pages)
    assert CAT2 not in r.next_pages


def test_search_grid_has_unknown_availability(fixture_text):
    url = f"{BASE}/it/SmartSearch/Search?q=pokemon%2030&selectedPagingField=18&selectedPage=1"
    r = _html(fixture_text("nop_search_grid.html"), url)
    assert r.kind == "search"
    assert len(r.products) == 4
    assert all(p.availability == Availability.UNKNOWN for p in r.products)
    bundle = next(p for p in r.products if p.product_id == "3766")
    assert bundle.price_value == 36.89
    assert r.total_pages == 2 and len(r.next_pages) == 1


def test_product_page_out_of_stock(fixture_text):
    url = f"{BASE}/it/pokemon-30-anniversario-mini-tin-case-sealed-ita"
    r = _html(fixture_text("nop_product_oos.html"), url)
    assert r.kind == "product"
    (p,) = r.products
    assert p.product_id == "3765"
    assert p.title == "Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA"
    assert p.availability == Availability.UNAVAILABLE  # back-in-stock subscription block present
    assert p.price_value == 129.89
    assert p.add_to_cart_url == f"{BASE}/it/addproducttocart/details/3765/1"


def test_product_page_available(fixture_text):
    url = f"{BASE}/it/pokemon-tin-da-collezione-megaforze-mega-darkrai-ex-ita"
    r = _html(fixture_text("nop_product_available.html"), url, hint="product")
    (p,) = r.products
    assert p.availability == Availability.AVAILABLE
    assert p.price == "€24,69"
    assert p.product_id == "3757"


def test_rss_feed(fixture_text):
    r = _html(fixture_text("nop_newproducts.xml"), f"{BASE}/newproducts/rss", ctype="application/rss+xml")
    assert r.kind == "rss"
    assert len(r.products) == 3
    assert r.products[0].product_id == "3821"
    assert r.products[1].title.startswith("Pokémon Storm Emerald")
    assert all(p.availability == Availability.UNKNOWN for p in r.products)


def test_shopify_products_json():
    text = '{"products":[{"id":1,"title":"Tin","handle":"tin","variants":[{"id":9,"price":"12.50","available":true}],"images":[]}]}'
    r = parse(FetchResult(url="https://shop.example/products.json", status=200, text=text, content_type="application/json"))
    assert r.kind == "shopify"
    (p,) = r.products
    assert p.availability == Availability.AVAILABLE
    assert p.url == "https://shop.example/products/tin"
    assert p.add_to_cart_url == "https://shop.example/cart/9:1"
    assert p.price_value == 12.5


def test_generic_json_ld_fallback():
    html = """<html><body><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Widget","url":"/p/widget",
     "offers":{"@type":"Offer","price":"19.90","availability":"https://schema.org/OutOfStock"}}
    </script></body></html>"""
    r = parse(FetchResult(url="https://other.example/p/widget", status=200, text=html, content_type="text/html"))
    (p,) = r.products
    assert p.title == "Widget" and p.url == "https://other.example/p/widget"
    assert p.availability == Availability.UNAVAILABLE
    assert p.price_value == 19.9


def test_parse_price_formats():
    assert parse_price("€129,89") == ("€129,89", 129.89)
    assert parse_price("€2299,69")[1] == 2299.69
    assert parse_price("1.299,00")[1] == 1299.0
    assert parse_price("€ 1.299")[1] == 1299.0
    assert parse_price("24.69")[1] == 24.69
    assert parse_price("  ") == (None, None)
    assert parse_price(None) == (None, None)
