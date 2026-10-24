"""Order placement and status transitions (stock side effects included)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import texts
from app.config import Settings
from app.database.models import (
    Customer,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    Product,
    ProductSize,
    ReservationStatus,
    StockReservation,
)
from app.database.session import SessionFactory
from app.services import inventory
from app.services.inventory import InsufficientStock, utcnow
from app.services.notifications import (
    KEYBOARD_ADMIN_ORDER,
    KEYBOARD_CLIENT_ORDER,
    enqueue,
    enqueue_admins,
)
from app.services.order_rules import CLIENT_CANCELLABLE, can_transition

log = logging.getLogger(__name__)

MAX_OPEN_ORDERS_PER_CLIENT = 3

__all__ = [
    "InsufficientStock",
    "OrderError",
    "ProductUnavailable",
    "TooManyOpenOrders",
    "InvalidTransition",
    "AlreadyInStatus",
    "OrderNotFound",
    "NotOrderOwner",
    "PlaceOrderResult",
    "place_order",
    "change_status",
    "cancel_by_client",
    "expire_overdue",
    "get_order",
]


class OrderError(Exception):
    pass


class ProductUnavailable(OrderError):
    pass


class TooManyOpenOrders(OrderError):
    pass


class OrderNotFound(OrderError):
    pass


class NotOrderOwner(OrderError):
    pass


class InvalidTransition(OrderError):
    # Plain values are copied: ORM objects expire when the transaction rolls back.
    def __init__(self, order: Order, target: str) -> None:
        super().__init__(f"order #{order.number}: {order.status} -> {target} is not allowed")
        self.number = order.number
        self.status = order.status
        self.target = target


class AlreadyInStatus(OrderError):
    def __init__(self, order: Order) -> None:
        super().__init__(f"order #{order.number} is already {order.status}")
        self.number = order.number
        self.status = order.status


@dataclass(slots=True)
class PlaceOrderResult:
    order: Order
    created: bool


def _order_options():
    return (selectinload(Order.items), selectinload(Order.reservations))


async def get_order(session: AsyncSession, *, order_id: int | None = None, number: int | None = None) -> Order | None:
    stmt = select(Order).options(*_order_options())
    if order_id is not None:
        stmt = stmt.where(Order.id == order_id)
    elif number is not None:
        stmt = stmt.where(Order.number == number)
    else:
        raise ValueError("order_id or number required")
    return await session.scalar(stmt)


async def _get_by_key(session: AsyncSession, key: str) -> Order | None:
    return await session.scalar(select(Order).options(*_order_options()).where(Order.idempotency_key == key))


async def _upsert_customer(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    telegram_name: str | None,
    full_name: str,
    contact: str,
) -> Customer:
    customer = await session.scalar(
        select(Customer).where(Customer.telegram_id == telegram_id).with_for_update()
    )
    if customer is None:
        customer = Customer(telegram_id=telegram_id)
        session.add(customer)
    customer.username = username
    customer.telegram_name = telegram_name
    customer.full_name = full_name
    customer.contact = contact
    customer.last_seen_at = utcnow()
    await session.flush()
    return customer


async def _create_order(
    session: AsyncSession,
    settings: Settings,
    *,
    telegram_id: int,
    username: str | None,
    telegram_name: str | None,
    product_id: int,
    size: Decimal,
    customer_name: str,
    contact: str,
    idempotency_key: str,
) -> Order:
    open_orders = await session.scalar(
        select(func.count(Order.id)).where(
            Order.telegram_id == telegram_id, Order.status == OrderStatus.PENDING
        )
    )
    if (open_orders or 0) >= MAX_OPEN_ORDERS_PER_CLIENT:
        raise TooManyOpenOrders()

    row = (
        await session.execute(
            select(ProductSize, Product)
            .join(Product, Product.id == ProductSize.product_id)
            .where(ProductSize.product_id == product_id, ProductSize.size == size)
        )
    ).first()
    if row is None or not row.Product.is_active:
        raise ProductUnavailable()
    size_row: ProductSize = row.ProductSize
    product: Product = row.Product

    customer = await _upsert_customer(
        session,
        telegram_id=telegram_id,
        username=username,
        telegram_name=telegram_name,
        full_name=customer_name,
        contact=contact,
    )
    quantity = 1
    price = Decimal(product.price)  # price fixed at the moment of purchase
    order = Order(
        customer_id=customer.id,
        telegram_id=telegram_id,
        username=username,
        customer_name=customer_name,
        contact=contact,
        status=OrderStatus.PENDING,
        total_amount=price * quantity,
        idempotency_key=idempotency_key,
        items=[
            OrderItem(
                product_id=product.id,
                product_size_id=size_row.id,
                brand=product.brand,
                model=product.model,
                sku=product.sku,
                color=product.color,
                size=size_row.size,
                unit_price=price,
                quantity=quantity,
                line_total=price * quantity,
            )
        ],
        reservations=[],
    )
    session.add(order)
    await session.flush()

    reservation = await inventory.reserve(
        session,
        product_size_id=size_row.id,
        quantity=quantity,
        order_id=order.id,
        ttl=timedelta(minutes=settings.reservation_ttl_minutes),
        actor=telegram_id,
    )
    order.reservations.append(reservation)
    session.add(OrderStatusHistory(order_id=order.id, from_status=None, to_status=OrderStatus.PENDING, actor_telegram_id=telegram_id))
    await enqueue_admins(
        session,
        settings,
        text=texts.admin_order_text(order),
        kind="admin_new_order",
        order_id=order.id,
        keyboard=KEYBOARD_ADMIN_ORDER,
    )
    await session.flush()
    return order


async def place_order(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    telegram_id: int,
    username: str | None,
    telegram_name: str | None,
    product_id: int,
    size: Decimal,
    customer_name: str,
    contact: str,
    idempotency_key: str,
) -> PlaceOrderResult:
    """Create an order exactly once per idempotency key, re-checking and reserving stock atomically.

    Raises InsufficientStock / ProductUnavailable / TooManyOpenOrders. Any failure rolls back the whole
    transaction, so a half-created order or a reservation without an order can never exist.
    """
    for _ in range(2):
        try:
            async with session_factory() as session, session.begin():
                existing = await _get_by_key(session, idempotency_key)
                if existing is not None:
                    return PlaceOrderResult(existing, created=False)
                order = await _create_order(
                    session,
                    settings,
                    telegram_id=telegram_id,
                    username=username,
                    telegram_name=telegram_name,
                    product_id=product_id,
                    size=size,
                    customer_name=customer_name,
                    contact=contact,
                    idempotency_key=idempotency_key,
                )
            log.info("Order #%s created (product %s, size %s)", order.number, product_id, size)
            return PlaceOrderResult(order, created=True)
        except IntegrityError as exc:
            # Concurrent double-submit with the same key: the other transaction won, read its order.
            if "idempotency_key" not in str(exc.orig):
                raise
            log.info("Duplicate order submit detected for key %s", idempotency_key[:8])
    async with session_factory() as session:
        existing = await _get_by_key(session, idempotency_key)
    if existing is None:  # pragma: no cover
        raise OrderError("order not found after idempotency conflict")
    return PlaceOrderResult(existing, created=False)


async def change_status(
    session: AsyncSession,
    settings: Settings,
    *,
    order_id: int,
    target: str,
    actor_id: int | None,
    comment: str | None = None,
    notify_client: bool = True,
) -> Order:
    """Move an order to ``target`` applying stock side effects. Runs in the caller's transaction."""
    order = await session.scalar(
        select(Order).options(*_order_options()).where(Order.id == order_id).with_for_update()
    )
    if order is None:
        raise OrderNotFound()
    if order.status == target:
        raise AlreadyInStatus(order)
    if not can_transition(order.status, target):
        raise InvalidTransition(order, target)

    previous = order.status
    active = [r for r in order.reservations if r.status == ReservationStatus.ACTIVE]
    if target == OrderStatus.CONFIRMED:
        new_expiry = utcnow() + timedelta(hours=settings.confirmed_reservation_ttl_hours)
        for reservation in active:
            reservation.expires_at = max(reservation.expires_at, new_expiry)
    elif target in (OrderStatus.PAID, OrderStatus.SHIPPED):
        for reservation in active:
            await inventory.commit_sale(session, reservation.id, actor=actor_id)
    elif target in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        for reservation in active:
            await inventory.release(session, reservation.id, expired=target == OrderStatus.EXPIRED, actor=actor_id)
        if previous == OrderStatus.PAID:
            # Stock was already written off at payment: bring the pairs back.
            for reservation in order.reservations:
                if reservation.status == ReservationStatus.CONSUMED:
                    await inventory.return_to_stock(
                        session,
                        product_size_id=reservation.product_size_id,
                        quantity=reservation.quantity,
                        order_id=order.id,
                        actor=actor_id,
                    )
                    reservation.status = ReservationStatus.RELEASED

    order.status = target
    order.status_changed_at = utcnow()
    session.add(
        OrderStatusHistory(
            order_id=order.id, from_status=previous, to_status=target, actor_telegram_id=actor_id, comment=comment
        )
    )
    if notify_client and target in texts.CLIENT_STATUS_MESSAGES:
        await enqueue(
            session,
            chat_id=order.telegram_id,
            text=texts.CLIENT_STATUS_MESSAGES[target].format(number=order.number),
            kind=f"client_{target}",
            order_id=order.id,
            keyboard=KEYBOARD_CLIENT_ORDER if target in CLIENT_CANCELLABLE else None,
        )
    await session.flush()
    log.info("Order #%s: %s -> %s (actor %s)", order.number, previous, target, actor_id)
    return order


async def cancel_by_client(session: AsyncSession, settings: Settings, *, order_id: int, telegram_id: int) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        raise OrderNotFound()
    if order.telegram_id != telegram_id:
        raise NotOrderOwner()
    if order.status not in CLIENT_CANCELLABLE:
        if order.status == OrderStatus.CANCELLED:
            raise AlreadyInStatus(order)
        raise InvalidTransition(order, OrderStatus.CANCELLED)
    order = await change_status(
        session, settings, order_id=order_id, target=OrderStatus.CANCELLED, actor_id=telegram_id,
        comment="cancelled by client", notify_client=False,
    )
    await enqueue_admins(
        session,
        settings,
        text=f"Клієнт скасував замовлення #{order.number}. Резерв знято, товар повернувся в наявність.",
        kind="admin_client_cancel",
        order_id=order.id,
    )
    return order


async def expire_overdue(session_factory: SessionFactory, settings: Settings, *, batch: int = 50) -> int:
    """Release reservations whose time is up and mark their orders as expired."""
    expired = 0
    async with session_factory() as session, session.begin():
        order_ids = (
            await session.scalars(
                select(StockReservation.order_id)
                .join(Order, Order.id == StockReservation.order_id)
                .where(
                    StockReservation.status == ReservationStatus.ACTIVE,
                    StockReservation.expires_at <= utcnow(),
                    Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]),
                )
                .order_by(StockReservation.expires_at)
                .limit(batch)
                .with_for_update(of=StockReservation, skip_locked=True)
            )
        ).all()
        for order_id in dict.fromkeys(order_ids):
            order = await change_status(
                session, settings, order_id=order_id, target=OrderStatus.EXPIRED, actor_id=None,
                comment="reservation expired",
            )
            await enqueue_admins(
                session,
                settings,
                text=f"⏰ Резерв замовлення #{order.number} минув — товар повернувся в наявність.",
                kind="admin_order_expired",
                order_id=order.id,
            )
            expired += 1
    if expired:
        log.info("Expired %s overdue orders", expired)
    return expired
