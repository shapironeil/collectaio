from drop_monitor.matching import Watch
from drop_monitor.models import Availability, Product, State
from drop_monitor.state import mark_absent, observe, state_from_availability
from drop_monitor.store import Store

W = Watch(keywords="Mini Tin Case")
URL = "https://shop.example/it/mini-tin-case"


def _p(av, price="€129,89", value=129.89):
    return Product(title="Mini Tin Case Sealed ITA", url=URL, availability=av, price=price, price_value=value, product_id="3765")


def test_state_from_availability():
    assert state_from_availability(Availability.AVAILABLE, None) == State.AVAILABLE
    assert state_from_availability(Availability.UNAVAILABLE, State.AVAILABLE) == State.PRESENT_UNAVAILABLE
    assert state_from_availability(Availability.UNKNOWN, None) == State.PRESENT_UNAVAILABLE
    assert state_from_availability(Availability.UNKNOWN, State.AVAILABLE) == State.AVAILABLE


def test_full_lifecycle_notifies_only_on_changes():
    store = Store(":memory:")
    t1 = observe(store, W, _p(Availability.UNAVAILABLE), "category", "https://shop.example/cat")
    assert (t1.old_state, t1.new_state, t1.changed) == (None, State.PRESENT_UNAVAILABLE, True)

    t2 = observe(store, W, _p(Availability.UNAVAILABLE), "category", "https://shop.example/cat")
    assert t2.changed is False and t2.price_changed is False

    t3 = observe(store, W, _p(Availability.AVAILABLE), "product", URL)
    assert (t3.old_state, t3.new_state, t3.changed) == (State.PRESENT_UNAVAILABLE, State.AVAILABLE, True)

    t4 = observe(store, W, _p(Availability.AVAILABLE, "€134,89", 134.89), "product", URL)
    assert t4.changed is False and t4.price_changed is True and t4.old_price == "€129,89"

    t5 = mark_absent(store, W, "HTTP 404 on product page")
    assert t5 is not None and t5.old_state == State.AVAILABLE and t5.new_state == State.ABSENT
    assert mark_absent(store, W, "still 404") is None  # no duplicate event

    t6 = observe(store, W, _p(Availability.AVAILABLE), "product", URL)
    assert (t6.old_state, t6.new_state) == (State.ABSENT, State.AVAILABLE)

    tracked = store.get_tracked(t6.tracked.watch_key)
    assert tracked.state == State.AVAILABLE and tracked.first_seen is not None
    events = store.recent_events(20)
    assert [e["new_state"] for e in reversed(events)] == ["present_unavailable", "available", "available", "absent", "available"]
    assert "price" in events[-2]["note"] or any("price" in e["note"] for e in events)


def test_unknown_availability_keeps_previous_state_but_updates_metadata():
    store = Store(":memory:")
    observe(store, W, _p(Availability.AVAILABLE), "product", URL)
    t = observe(store, W, Product(title="Mini Tin Case Sealed ITA", url=URL, availability=Availability.UNKNOWN), "rss", "https://shop.example/rss")
    assert t.new_state == State.AVAILABLE and t.changed is False
    assert store.get_tracked(t.tracked.watch_key).price == "€129,89"  # kept from the previous observation
