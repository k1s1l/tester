"""Scenarios 9, 10, 12, 14, 15 and the order status machine."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.database.models import (
    MovementKind,
    Notification,
    Order,
    OrderStatus,
    Product,
    ProductSize,
    ReservationStatus,
    StockMovement,
    StockReservation,
)
from app.services import inventory
from app.services import orders as orders_service
from app.services.orders import (
    AlreadyInStatus,
    InsufficientStock,
    InvalidTransition,
    NotOrderOwner,
    cancel_by_client,
    change_status,
    expire_overdue,
    place_order,
)
from tests.conftest import ADMIN_CHAT, ADMIN_ID, CLIENT_ID


async def _product_id(session_factory, sku: str = "NV5") -> int:
    async with session_factory() as session:
        return (await session.scalar(select(Product.id).where(Product.sku == sku)))  # type: ignore[return-value]


async def _size(session_factory, sku: str, size: str) -> ProductSize:
    async with session_factory() as session:
        return await session.scalar(
            select(ProductSize).join(Product).where(Product.sku == sku, ProductSize.size == Decimal(size))
        )  # type: ignore[return-value]


async def _place(session_factory, settings, *, size: str = "47", key: str | None = None, telegram_id: int = CLIENT_ID):
    return await place_order(
        session_factory,
        settings,
        telegram_id=telegram_id,
        username="client",
        telegram_name="Test Client",
        product_id=await _product_id(session_factory),
        size=Decimal(size),
        customer_name="Микита",
        contact="+380671234567",
        idempotency_key=key or uuid.uuid4().hex,
    )


async def test_place_order_reserves_and_snapshots(session_factory, settings):
    result = await _place(session_factory, settings)
    order = result.order
    assert result.created and order.number == 1001 and order.status == OrderStatus.PENDING
    assert order.total_amount == Decimal("5800.00")
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved, row.available) == (1, 1, 0)
    async with session_factory() as session:
        notes = (await session.scalars(select(Notification))).all()
        # Price change after ordering must not affect the order
        await session.execute(update(Product).where(Product.sku == "NV5").values(price=9999))
        await session.commit()
        saved = await orders_service.get_order(session, order_id=order.id)
    assert saved.items[0].unit_price == Decimal("5800.00") and saved.total_amount == Decimal("5800.00")
    assert [n.chat_id for n in notes] == [ADMIN_CHAT]
    assert "НОВЕ ЗАМОВЛЕННЯ #1001" in notes[0].text and "Артикул: NV5" in notes[0].text


# 9. Two clients try to buy the last pair at the same time
async def test_concurrent_last_pair(session_factory, settings):
    results = await asyncio.gather(
        _place(session_factory, settings, telegram_id=1001),
        _place(session_factory, settings, telegram_id=1002),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1 and len(failures) == 1
    assert isinstance(failures[0], InsufficientStock)
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 1)
    async with session_factory() as session:
        assert len((await session.scalars(select(Order))).all()) == 1


async def test_many_concurrent_buyers_never_oversell(session_factory, settings):
    # Size 42 has 3 pairs; 8 buyers race for it.
    results = await asyncio.gather(
        *[_place(session_factory, settings, size="42", telegram_id=2000 + i) for i in range(8)],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 3
    row = await _size(session_factory, "NV5", "42")
    assert (row.quantity, row.reserved) == (3, 3)


async def test_size_out_of_stock_rejected(session_factory, settings):
    with pytest.raises(InsufficientStock):
        await _place(session_factory, settings, size="44")


# 12. Confirmation button pressed twice
async def test_idempotent_order_creation(session_factory, settings):
    key = uuid.uuid4().hex
    first = await _place(session_factory, settings, size="42", key=key)
    second = await _place(session_factory, settings, size="42", key=key)
    parallel = await asyncio.gather(*[_place(session_factory, settings, size="42", key=key) for _ in range(3)])
    assert first.created and not second.created
    assert all(not r.created for r in parallel)
    assert {first.order.id, second.order.id, *[r.order.id for r in parallel]} == {first.order.id}
    row = await _size(session_factory, "NV5", "42")
    assert row.reserved == 1


async def test_admin_double_click_on_status(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    async with session_factory() as session, session.begin():
        await change_status(session, settings, order_id=order.id, target=OrderStatus.CONFIRMED, actor_id=ADMIN_ID)
    with pytest.raises(AlreadyInStatus):
        async with session_factory() as session, session.begin():
            await change_status(session, settings, order_id=order.id, target=OrderStatus.CONFIRMED, actor_id=ADMIN_ID)


async def test_status_machine_and_stock_write_off(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    with pytest.raises(InvalidTransition):
        async with session_factory() as session, session.begin():
            await change_status(session, settings, order_id=order.id, target=OrderStatus.SHIPPED, actor_id=ADMIN_ID)
    for target in (OrderStatus.CONFIRMED, OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        async with session_factory() as session, session.begin():
            await change_status(session, settings, order_id=order.id, target=target, actor_id=ADMIN_ID)
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (0, 0)  # written off exactly once
    async with session_factory() as session:
        kinds = (await session.scalars(select(StockMovement.kind).where(StockMovement.order_id == order.id))).all()
    assert kinds.count(MovementKind.SALE) == 1
    with pytest.raises(InvalidTransition):
        async with session_factory() as session, session.begin():
            await change_status(session, settings, order_id=order.id, target=OrderStatus.CANCELLED, actor_id=ADMIN_ID)


async def test_cancel_after_payment_returns_stock(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    for target in (OrderStatus.CONFIRMED, OrderStatus.PAID, OrderStatus.CANCELLED):
        async with session_factory() as session, session.begin():
            await change_status(session, settings, order_id=order.id, target=target, actor_id=ADMIN_ID)
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 0)


async def test_admin_reject_releases_reservation(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    async with session_factory() as session, session.begin():
        await change_status(session, settings, order_id=order.id, target=OrderStatus.REJECTED, actor_id=ADMIN_ID)
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 0)


# 10. Client cancels the order
async def test_client_cancels_order(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    with pytest.raises(NotOrderOwner):
        async with session_factory() as session, session.begin():
            await cancel_by_client(session, settings, order_id=order.id, telegram_id=999)
    async with session_factory() as session, session.begin():
        cancelled = await cancel_by_client(session, settings, order_id=order.id, telegram_id=CLIENT_ID)
    assert cancelled.status == OrderStatus.CANCELLED
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 0)
    async with session_factory() as session:
        reservation = await session.scalar(select(StockReservation).where(StockReservation.order_id == order.id))
        admin_notes = (
            await session.scalars(select(Notification.text).where(Notification.kind == "admin_client_cancel"))
        ).all()
    assert reservation.status == ReservationStatus.RELEASED
    assert admin_notes and "скасував замовлення #1001" in admin_notes[0]
    # The pair is available again for another client
    assert (await _place(session_factory, settings, telegram_id=777)).created


# 15. Reservation expires
async def test_reservation_expiry(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    assert await expire_overdue(session_factory, settings) == 0  # not yet expired
    async with session_factory() as session, session.begin():
        await session.execute(
            update(StockReservation)
            .where(StockReservation.order_id == order.id)
            .values(expires_at=inventory.utcnow() - timedelta(minutes=1))
        )
    assert await expire_overdue(session_factory, settings) == 1
    assert await expire_overdue(session_factory, settings) == 0  # idempotent
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 0)
    async with session_factory() as session:
        saved = await session.get(Order, order.id)
        kinds = (await session.scalars(select(StockMovement.kind).where(StockMovement.order_id == order.id))).all()
        client_note = await session.scalar(select(Notification).where(Notification.kind == "client_expired"))
    assert saved.status == OrderStatus.EXPIRED
    assert MovementKind.EXPIRE in kinds
    assert client_note is not None and client_note.chat_id == CLIENT_ID


async def test_confirmed_order_gets_longer_reservation(session_factory, settings):
    order = (await _place(session_factory, settings)).order
    async with session_factory() as session, session.begin():
        await change_status(session, settings, order_id=order.id, target=OrderStatus.CONFIRMED, actor_id=ADMIN_ID)
    async with session_factory() as session:
        reservation = await session.scalar(select(StockReservation).where(StockReservation.order_id == order.id))
    assert reservation.expires_at > inventory.utcnow() + timedelta(hours=settings.confirmed_reservation_ttl_hours - 1)


# 14. Database error in the middle of order creation -> full rollback
async def test_db_error_rolls_back_everything(session_factory, settings, monkeypatch):
    async def broken_enqueue(*args, **kwargs):
        raise IntegrityError("INSERT INTO notifications", {}, Exception("simulated failure"))

    monkeypatch.setattr(orders_service, "enqueue_admins", broken_enqueue)
    with pytest.raises(IntegrityError):
        await _place(session_factory, settings)
    row = await _size(session_factory, "NV5", "47")
    assert (row.quantity, row.reserved) == (1, 0)
    async with session_factory() as session:
        assert (await session.scalars(select(Order))).all() == []
        assert (await session.scalars(select(StockReservation))).all() == []


async def test_database_forbids_negative_stock(session_factory):
    row = await _size(session_factory, "NV5", "44")
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(update(ProductSize).where(ProductSize.id == row.id).values(quantity=-1))
    with pytest.raises(inventory.InvalidStockChange):
        async with session_factory() as session, session.begin():
            await inventory.set_quantity(session, product_id=row.product_id, size=row.size, quantity=-5)


async def test_set_quantity_cannot_go_below_reserved(session_factory, settings):
    await _place(session_factory, settings, size="42")
    row = await _size(session_factory, "NV5", "42")
    with pytest.raises(inventory.InvalidStockChange):
        async with session_factory() as session, session.begin():
            await inventory.set_quantity(session, product_id=row.product_id, size=row.size, quantity=0)
