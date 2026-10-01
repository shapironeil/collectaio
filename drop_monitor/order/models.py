from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Task:
    """A checkout task: which product, for which buyer profiles, with which limits (config.yaml -> tasks)."""

    name: str
    product: str  # keywords or name of an entry in `products`
    profiles: list[str] = field(default_factory=lambda: ["default"])
    quantity: int = 1
    mode: str = "monitor"  # monitor | auto_checkout
    max_total_eur: float = 150.0
    confirm_on_telegram: bool = True
    confirm_timeout_seconds: int = 300
    shipping_method: str = ""  # substring of the option label; empty = first offered
    payment_method: str = ""  # substring of the option label/system name; empty = first offered
    enabled: bool = True


@dataclass
class StepLog:
    step: str
    ok: bool
    detail: str = ""


@dataclass
class OrderResult:
    task: str
    profile: str
    status: str  # placed | pending_payment | dry_run | cancelled | over_limit | failed | timeout
    total_eur: Optional[float] = None
    message: str = ""
    order_url: Optional[str] = None  # completed page or external payment page (PayPal)
    steps: list[StepLog] = field(default_factory=list)

    def log(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append(StepLog(step, ok, detail))
