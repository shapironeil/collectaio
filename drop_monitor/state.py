"""State machine: turn observations into state transitions and notification events."""
from __future__ import annotations

from dataclasses import dataclass

from drop_monitor.matching import Watch
from drop_monitor.models import Availability, Product, State
from drop_monitor.store import Store, Tracked, utcnow


@dataclass
class Transition:
    watch: Watch
    tracked: Tracked
    old_state: State | None
    new_state: State
    reason: str
    price_changed: bool = False
    old_price: str | None = None

    @property
    def changed(self) -> bool:
        return self.old_state != self.new_state


def watch_key(watch: Watch) -> str:
    return watch.url or watch.keywords.strip().lower()


def state_from_availability(av: Availability, current: State | None) -> State:
    if av == Availability.AVAILABLE:
        return State.AVAILABLE
    if av == Availability.UNAVAILABLE:
        return State.PRESENT_UNAVAILABLE
    # UNKNOWN: the source proves presence but not purchasability.
    if current in (State.AVAILABLE, State.PRESENT_UNAVAILABLE):
        return current
    return State.PRESENT_UNAVAILABLE


def observe(store: Store, watch: Watch, product: Product, source_kind: str, source_url: str) -> Transition:
    """Record that `product` (which matches `watch`) was seen on `source_kind`."""
    key = watch_key(watch)
    prev = store.get_tracked(key)
    old_state = prev.state if prev else None
    new_state = state_from_availability(product.availability, old_state)
    now = utcnow()

    # A listing page can be stale compared to a product page we fetched a moment ago,
    # but on this kind of shop both come from the same server-side stock figure, so
    # the latest observation always wins.
    tracked = Tracked(
        watch_key=key,
        label=watch.label,
        state=new_state,
        title=product.title,
        url=product.url or (prev.url if prev else None),
        product_id=product.product_id or (prev.product_id if prev else None),
        price=product.price or (prev.price if prev else None),
        price_value=product.price_value if product.price_value is not None else (prev.price_value if prev else None),
        add_to_cart_url=product.add_to_cart_url or (prev.add_to_cart_url if prev else None),
        source_kind=source_kind,
        source_url=source_url,
        first_seen=(prev.first_seen if prev and prev.first_seen else now),
        last_seen=now,
        last_checked=now,
    )
    price_changed = bool(
        prev and prev.price_value is not None and product.price_value is not None and abs(prev.price_value - product.price_value) > 0.004
    )
    store.upsert_tracked(tracked)
    if old_state != new_state:
        store.add_event(key, old_state, new_state, tracked.price, note=f"seen on {source_kind}")
    elif price_changed:
        store.add_event(key, old_state, new_state, tracked.price, note=f"price {prev.price} -> {tracked.price}")
    return Transition(watch, tracked, old_state, new_state, reason=f"seen on {source_kind}", price_changed=price_changed, old_price=prev.price if prev else None)


def mark_absent(store: Store, watch: Watch, reason: str) -> Transition | None:
    """Product disappeared (404 on its page, or missing from a complete listing sweep)."""
    key = watch_key(watch)
    prev = store.get_tracked(key)
    now = utcnow()
    if prev is None:
        tracked = Tracked(watch_key=key, label=watch.label, state=State.ABSENT, url=watch.url, first_seen=None, last_checked=now)
        store.upsert_tracked(tracked)
        store.add_event(key, None, State.ABSENT, None, note=reason)
        return Transition(watch, tracked, None, State.ABSENT, reason)
    if prev.state == State.ABSENT:
        prev.last_checked = now
        store.upsert_tracked(prev)
        return None
    old = prev.state
    prev.state = State.ABSENT
    prev.last_checked = now
    store.upsert_tracked(prev)
    store.add_event(key, old, State.ABSENT, prev.price, note=reason)
    return Transition(watch, prev, old, State.ABSENT, reason)


def touch_checked(store: Store, watch: Watch) -> None:
    prev = store.get_tracked(watch_key(watch))
    if prev:
        prev.last_checked = utcnow()
        store.upsert_tracked(prev)
