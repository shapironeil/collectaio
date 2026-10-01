"""Drives one checkout per buyer profile, with limits, dry-run and human confirmation."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from drop_monitor.order.cart import Cart, CartError
from drop_monitor.order.checkout import CheckoutError, OnePageCheckout
from drop_monitor.order.models import OrderResult, Task
from drop_monitor.order.session import LoginError, ShopSession

log = logging.getLogger(__name__)


class Confirmer:
    """Asks a human before placing an order. Default: console; Telegram variant in notifier."""

    def ask(self, text: str, timeout: int) -> bool | None:  # True yes, False no, None timeout
        try:
            ans = input(f"\n{text}\nConfermi l'ordine? [s/N] ").strip().lower()
        except EOFError:
            return None
        return ans in ("s", "si", "sì", "y", "yes")


class Prober:
    """Saves every page of a checkout run under data/probes/<stamp>/ for study (tokens redacted)."""

    def __init__(self, root: str | Path):
        self.dir = Path(root) / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.n = 0

    def __call__(self, label: str, r) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n += 1
        safe = re.sub(r"[^A-Za-z0-9]+", "_", label)[:60]
        ext = "json" if "json" in (r.headers.get("content-type") or "") else "html"
        text = re.sub(r'(__RequestVerificationToken"[^>]*value=")[^"]+', r"\1REDACTED", r.text)
        (self.dir / f"{self.n:02d}-{safe}-{r.status_code}.{ext}").write_text(f"<!-- {r.url} -->\n{text}", encoding="utf-8")


class OrderRunner:
    def __init__(self, base_url: str, user_agent: str, profiles, store, confirmer: Confirmer | None = None, notify=None, sessions_dir="personal/sessions", probe_dir: str | None = None, proxies: list[str] | None = None):
        self.base_url = base_url
        self.proxies = list(proxies or [])  # sticky: profile i uses proxy i (mod len)
        self.ua = user_agent
        self.profiles = profiles  # ProfileStore
        self.store = store
        self.confirmer = confirmer or Confirmer()
        self.notify = notify or (lambda text: None)
        self.sessions_dir = sessions_dir
        self.probe_dir = probe_dir
        self._running: set[str] = set()
        self._lock = threading.Lock()

    # ---- entry points ------------------------------------------------------
    def run_task(self, task: Task, product_id: str, product_title: str, dry_run: bool = False, only_profile: str | None = None) -> list[OrderResult]:
        key = f"{task.name}:{product_id}"
        with self._lock:
            if key in self._running:
                log.info("task %s already running for product %s", task.name, product_id)
                return []
            self._running.add(key)
        try:
            results = []
            for name in task.profiles:
                if only_profile and name != only_profile:
                    continue
                if task.max_checkouts and not dry_run and self.store.count_orders(task.name) >= task.max_checkouts:
                    log.info("task %s reached max_checkouts=%d: skipping profile %s", task.name, task.max_checkouts, name)
                    results.append(OrderResult(task=task.name, profile=name, status="capped", message=f"limite di {task.max_checkouts} checkout raggiunto"))
                    continue
                res = self.run_one(task, name, product_id, product_title, dry_run=dry_run)
                results.append(res)
                self._record(res, product_title)
                if res.status in ("placed", "pending_payment", "dry_run"):
                    continue
                if res.status in ("over_limit", "cancelled", "timeout"):
                    continue
            return results
        finally:
            with self._lock:
                self._running.discard(key)

    def run_one(self, task: Task, profile_name: str, product_id: str, product_title: str, dry_run: bool = False) -> OrderResult:
        res = OrderResult(task=task.name, profile=profile_name, status="failed")
        prober = Prober(self.probe_dir) if self.probe_dir else None
        try:
            profile = self.profiles.load(profile_name)
        except Exception as e:
            res.message = f"profilo non caricabile: {e}"
            return res
        proxy = None
        if self.proxies:
            idx = task.profiles.index(profile_name) if profile_name in task.profiles else 0
            proxy = self.proxies[idx % len(self.proxies)]
            res.log("proxy", True, proxy.split("@")[-1])
        session = ShopSession(self.base_url, profile, self.ua, self.sessions_dir, probe=prober, proxy=proxy)
        try:
            self._flow(task, profile, session, product_id, product_title, dry_run, res)
        except (LoginError, CartError, CheckoutError) as e:
            res.status = "failed"
            res.message = str(e)
            res.log("errore", False, str(e))
            log.warning("order %s/%s failed: %s", task.name, profile_name, e)
        except Exception as e:  # network etc.
            res.status = "failed"
            res.message = f"{type(e).__name__}: {e}"
            log.exception("order %s/%s crashed", task.name, profile_name)
        finally:
            session.close()
            if prober and prober.n:
                res.log("probe", True, f"pagine salvate in {prober.dir}")
        return res

    # ---- the flow ------------------------------------------------------------
    def _flow(self, task: Task, profile: dict, session: ShopSession, product_id: str, product_title: str, dry_run: bool, res: OrderResult) -> None:
        session.ensure_logged_in()
        res.log("login", True, profile["account"].get("email", ""))
        cart = Cart(session)
        cart.clear()
        msg = cart.add(product_id, task.quantity)
        res.log("carrello", True, msg)
        view = cart.view()
        if not view.items:
            raise CartError("il carrello e' vuoto dopo l'aggiunta (prodotto esaurito nel frattempo?)")
        if view.warnings:
            raise CartError("avvisi carrello: " + "; ".join(view.warnings))
        if view.quantity != task.quantity:
            res.log("quantita", False, f"richiesta {task.quantity}, nel carrello {view.quantity}")
        url = cart.proceed_to_checkout(view)
        if "/login" in url.lower():
            raise CheckoutError("il sito ha chiesto di nuovo il login al checkout")
        opc = OnePageCheckout(session)
        opc.start()
        addr = opc.choose_address(profile)
        step = opc.save_billing(profile, addr)
        res.log("indirizzo", True, "esistente" if addr else "nuovo")
        html = step.html
        # Follow goto_section until the confirm section is loaded.
        shipping_label = payment_label = ""
        for _ in range(6):
            sec = (step.section or "").replace("-", "_")
            if step.redirect:
                raise CheckoutError(f"redirect inatteso durante il checkout: {step.redirect}")
            if sec == "shipping":
                step = opc._post("shipping", {"shipping_address_id": addr or "", **opc._hidden(None)})
            elif sec == "shipping_method":
                step, opt = opc.save_shipping_method(step.html, task.shipping_method or profile.get("preferences", {}).get("shipping_method", ""))
                shipping_label = opt.label
                res.log("spedizione", True, opt.label)
            elif sec == "payment_method":
                step, opt = opc.save_payment_method(step.html, task.payment_method or profile.get("preferences", {}).get("payment_method", ""))
                payment_label = opt.label
                res.log("pagamento", True, opt.label)
            elif sec == "payment_info":
                step = opc.save_payment_info(step.html)
            elif sec == "confirm_order":
                html = step.html
                break
            else:
                raise CheckoutError(f"sezione checkout sconosciuta: {step.section!r}")
        else:
            raise CheckoutError("il checkout non e' arrivato alla conferma")
        summary = opc.summary(html)
        total = summary.total if summary.total is not None else (view.total or view.subtotal)
        res.total_eur = total
        res.log("riepilogo", True, f"totale {total} € · {shipping_label} · {payment_label}")
        if summary.warnings:
            raise CheckoutError("avvisi al riepilogo: " + "; ".join(summary.warnings))
        limit = float(task.max_total_eur or profile.get("preferences", {}).get("max_total_eur") or 0)
        if total is None:
            raise CheckoutError("totale ordine non leggibile dal riepilogo: non confermo")
        if limit and total > limit:
            res.status = "over_limit"
            res.message = f"totale {total:.2f} € oltre il limite {limit:.2f} €"
            return
        text = (f"🛒 Ordine pronto — task {task.name}, profilo {res.profile}\n{product_title} × {task.quantity}\n"
                f"Totale: {total:.2f} € · {shipping_label} · {payment_label}")
        if dry_run:
            res.status = "dry_run"
            res.message = "fermato prima della conferma (dry-run)"
            self.notify("🧪 " + text + "\n(dry-run: nessun ordine inviato)")
            return
        if task.confirm_on_telegram or profile.get("preferences", {}).get("confirm_on_telegram", True):
            ans = self.confirmer.ask(text, task.confirm_timeout_seconds)
            if ans is None:
                res.status = "timeout"
                res.message = "nessuna conferma entro il tempo limite"
                return
            if not ans:
                res.status = "cancelled"
                res.message = "annullato dall'utente"
                return
        final = opc.confirm(html)
        if final.redirect:
            res.order_url = session.url(final.redirect)
            if "completed" in final.redirect.lower():
                res.status = "placed"
                res.message = "ordine inviato"
            else:
                res.status = "pending_payment"
                res.message = "ordine creato: completa il pagamento al link"
        else:
            res.status = "placed"
            res.message = "ordine inviato"
        res.log("conferma", True, res.message)
        self.notify(f"✅ {res.message} — {product_title} × {task.quantity} · {total:.2f} € · profilo {res.profile}" + (f"\n🔗 {res.order_url}" if res.order_url else ""))

    def _record(self, res: OrderResult, product_title: str) -> None:
        try:
            self.store.add_order(res.task, res.profile, product_title, res.status, res.total_eur, res.order_url, res.message)
        except Exception:
            log.exception("could not record order")
