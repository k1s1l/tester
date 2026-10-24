"""Order status machine (no dependencies, shared by services and keyboards)."""

from __future__ import annotations

from app.database.models import OrderStatus

S = OrderStatus

TRANSITIONS: dict[str, frozenset[str]] = {
    S.PENDING: frozenset({S.CONFIRMED, S.REJECTED, S.CANCELLED, S.EXPIRED}),
    S.CONFIRMED: frozenset({S.PAID, S.SHIPPED, S.CANCELLED, S.EXPIRED}),
    S.PAID: frozenset({S.SHIPPED, S.CANCELLED}),
    S.SHIPPED: frozenset({S.COMPLETED}),
    S.COMPLETED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
    S.EXPIRED: frozenset(),
}

# Statuses an admin can set manually via buttons (EXPIRED is set by the background job only).
ADMIN_TARGETS: tuple[str, ...] = (S.CONFIRMED, S.REJECTED, S.PAID, S.SHIPPED, S.COMPLETED, S.CANCELLED)

# A client may cancel their own order only before payment/shipping.
CLIENT_CANCELLABLE: frozenset[str] = frozenset({S.PENDING, S.CONFIRMED})

# Statuses in which the order holds stock (reserved or written off) and counts as "open".
OPEN_STATUSES: frozenset[str] = frozenset({S.PENDING, S.CONFIRMED, S.PAID, S.SHIPPED})
FINAL_STATUSES: frozenset[str] = frozenset({S.COMPLETED, S.REJECTED, S.CANCELLED, S.EXPIRED})
REVENUE_STATUSES: frozenset[str] = frozenset({S.PAID, S.SHIPPED, S.COMPLETED})


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def admin_actions(current: str) -> list[str]:
    return [target for target in ADMIN_TARGETS if can_transition(current, target)]
