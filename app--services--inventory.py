"""Warehouse operations. Every function runs inside the caller's transaction.

Lifecycle of one pair:
    available --reserve()--> reserved --commit_sale()--> sold (quantity decreased)
                                 \\--release()-------> available again (cancel/reject/expire)
    sold --return_to_stock()--> available again (cancel after payment)

All stock updates are single conditional UPDATE statements, so two concurrent buyers can never
both take the last pair: PostgreSQL serialises the row update and re-checks the WHERE clause.
CHECK constraints in the schema additionally make negative stock impossible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    MovementKind,
    ProductSize,
    ReservationStatus,
    StockMovement,
    StockReservation,
)


class InventoryError(Exception):
    pass


class InsufficientStock(InventoryError):
    pass


class InvalidStockChange(InventoryError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _movement(
    size_id: int,
    kind: MovementKind,
    *,
    quantity_after: int,
    reserved_after: int,
    delta_quantity: int = 0,
    delta_reserved: int = 0,
    order_id: int | None = None,
    actor: int | None = None,
    comment: str | None = None,
) -> StockMovement:
    return StockMovement(
        product_size_id=size_id,
        order_id=order_id,
        kind=kind,
        delta_quantity=delta_quantity,
        delta_reserved=delta_reserved,
        quantity_after=quantity_after,
        reserved_after=reserved_after,
        actor_telegram_id=actor,
        comment=comment[:256] if comment else None,
    )


async def reserve(
    session: AsyncSession,
    *,
    product_size_id: int,
    quantity: int,
    order_id: int,
    ttl: timedelta,
    actor: int | None = None,
) -> StockReservation:
    if quantity <= 0:
        raise InvalidStockChange("quantity must be positive")
    stmt = (
        update(ProductSize)
        .where(
            ProductSize.id == product_size_id,
            ProductSize.quantity - ProductSize.reserved >= quantity,
        )
        .values(reserved=ProductSize.reserved + quantity)
        .returning(ProductSize.quantity, ProductSize.reserved)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise InsufficientStock(f"size {product_size_id}: not enough stock for {quantity}")
    reservation = StockReservation(
        order_id=order_id,
        product_size_id=product_size_id,
        quantity=quantity,
        status=ReservationStatus.ACTIVE,
        expires_at=utcnow() + ttl,
    )
    session.add(reservation)
    session.add(
        _movement(
            product_size_id,
            MovementKind.RESERVE,
            delta_reserved=quantity,
            quantity_after=row.quantity,
            reserved_after=row.reserved,
            order_id=order_id,
            actor=actor,
        )
    )
    await session.flush()
    return reservation


async def _lock_reservation(session: AsyncSession, reservation_id: int) -> StockReservation:
    reservation = await session.scalar(
        select(StockReservation).where(StockReservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise InventoryError(f"reservation {reservation_id} not found")
    return reservation


async def release(
    session: AsyncSession,
    reservation_id: int,
    *,
    expired: bool = False,
    actor: int | None = None,
) -> bool:
    """Return reserved pairs to available stock. Idempotent: returns False if already closed."""
    reservation = await _lock_reservation(session, reservation_id)
    if reservation.status != ReservationStatus.ACTIVE:
        return False
    row = (
        await session.execute(
            update(ProductSize)
            .where(ProductSize.id == reservation.product_size_id, ProductSize.reserved >= reservation.quantity)
            .values(reserved=ProductSize.reserved - reservation.quantity)
            .returning(ProductSize.quantity, ProductSize.reserved)
        )
    ).first()
    if row is None:  # pragma: no cover - would indicate corrupted counters
        raise InvalidStockChange("reserved counter is lower than reservation quantity")
    reservation.status = ReservationStatus.EXPIRED if expired else ReservationStatus.RELEASED
    reservation.closed_at = utcnow()
    session.add(
        _movement(
            reservation.product_size_id,
            MovementKind.EXPIRE if expired else MovementKind.RELEASE,
            delta_reserved=-reservation.quantity,
            quantity_after=row.quantity,
            reserved_after=row.reserved,
            order_id=reservation.order_id,
            actor=actor,
        )
    )
    await session.flush()
    return True


async def commit_sale(session: AsyncSession, reservation_id: int, *, actor: int | None = None) -> bool:
    """Write off reserved pairs (payment received or parcel shipped). Idempotent."""
    reservation = await _lock_reservation(session, reservation_id)
    if reservation.status != ReservationStatus.ACTIVE:
        return False
    row = (
        await session.execute(
            update(ProductSize)
            .where(
                ProductSize.id == reservation.product_size_id,
                ProductSize.reserved >= reservation.quantity,
                ProductSize.quantity >= reservation.quantity,
            )
            .values(
                quantity=ProductSize.quantity - reservation.quantity,
                reserved=ProductSize.reserved - reservation.quantity,
            )
            .returning(ProductSize.quantity, ProductSize.reserved)
        )
    ).first()
    if row is None:  # pragma: no cover
        raise InvalidStockChange("stock counters are inconsistent with the reservation")
    reservation.status = ReservationStatus.CONSUMED
    reservation.closed_at = utcnow()
    session.add(
        _movement(
            reservation.product_size_id,
            MovementKind.SALE,
            delta_quantity=-reservation.quantity,
            delta_reserved=-reservation.quantity,
            quantity_after=row.quantity,
            reserved_after=row.reserved,
            order_id=reservation.order_id,
            actor=actor,
        )
    )
    await session.flush()
    return True


async def return_to_stock(
    session: AsyncSession, *, product_size_id: int, quantity: int, order_id: int | None, actor: int | None
) -> None:
    row = (
        await session.execute(
            update(ProductSize)
            .where(ProductSize.id == product_size_id)
            .values(quantity=ProductSize.quantity + quantity)
            .returning(ProductSize.quantity, ProductSize.reserved)
        )
    ).first()
    if row is None:
        raise InventoryError("product size not found")
    session.add(
        _movement(
            product_size_id,
            MovementKind.RETURN,
            delta_quantity=quantity,
            quantity_after=row.quantity,
            reserved_after=row.reserved,
            order_id=order_id,
            actor=actor,
        )
    )
    await session.flush()


async def set_quantity(
    session: AsyncSession,
    *,
    product_id: int,
    size: Decimal,
    quantity: int,
    actor: int | None = None,
    comment: str | None = None,
) -> tuple[ProductSize, int]:
    """Set the physical quantity for a size (creates the size if needed).

    Returns (size_row, previous_available). Refuses to go below the currently reserved amount.
    """
    if quantity < 0:
        raise InvalidStockChange("Кількість не може бути від'ємною")
    size_row = await session.scalar(
        select(ProductSize)
        .where(ProductSize.product_id == product_id, ProductSize.size == size)
        .with_for_update()
    )
    if size_row is None:
        size_row = ProductSize(product_id=product_id, size=size, quantity=quantity, reserved=0)
        session.add(size_row)
        await session.flush()
        session.add(
            _movement(
                size_row.id,
                MovementKind.INITIAL,
                delta_quantity=quantity,
                quantity_after=quantity,
                reserved_after=0,
                actor=actor,
                comment=comment,
            )
        )
        await session.flush()
        return size_row, 0
    if quantity < size_row.reserved:
        raise InvalidStockChange(
            f"Не можна встановити {quantity}: {size_row.reserved} пар(и) зарезервовано під замовлення"
        )
    previous_available = size_row.available
    delta = quantity - size_row.quantity
    if delta:
        size_row.quantity = quantity
        session.add(
            _movement(
                size_row.id,
                MovementKind.ADJUST,
                delta_quantity=delta,
                quantity_after=quantity,
                reserved_after=size_row.reserved,
                actor=actor,
                comment=comment,
            )
        )
    await session.flush()
    return size_row, previous_available


async def get_size_row(session: AsyncSession, product_id: int, size: Decimal) -> ProductSize | None:
    return await session.scalar(
        select(ProductSize).where(ProductSize.product_id == product_id, ProductSize.size == size)
    )
