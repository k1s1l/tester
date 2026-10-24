"""ORM models.

Stock model
-----------
``ProductSize.quantity`` is the physical number of pairs in the warehouse.
``ProductSize.reserved`` is how many of them are held by active order reservations.
Available for sale = quantity - reserved. Both are protected by CHECK constraints
(non-negative, reserved <= quantity), so the database itself refuses any operation
that would produce a negative or oversold stock.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin


class OrderStatus(StrEnum):
    PENDING = "pending"        # created by the client, stock reserved, waiting for admin
    CONFIRMED = "confirmed"    # admin confirmed, reservation extended
    PAID = "paid"              # payment confirmed by admin -> stock written off
    SHIPPED = "shipped"        # handed to delivery -> stock written off (if not yet)
    COMPLETED = "completed"
    REJECTED = "rejected"      # admin rejected -> reservation released
    CANCELLED = "cancelled"    # client/admin cancelled -> reservation released / stock returned
    EXPIRED = "expired"        # reservation expired -> reservation released


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    CONSUMED = "consumed"      # turned into a sale (stock written off)
    RELEASED = "released"      # returned to available stock (cancel / reject)
    EXPIRED = "expired"        # returned to available stock by the expiry job


class MovementKind(StrEnum):
    INITIAL = "initial"
    ADJUST = "adjust"          # manual change by admin / catalog import
    RESERVE = "reserve"
    RELEASE = "release"
    EXPIRE = "expire"
    SALE = "sale"
    RETURN = "return"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def _enum_check(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{item.value}'" for item in enum)
    return f"{column} IN ({values})"


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        Index("ix_products_brand_model", "brand", "model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="lifestyle")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sizes: Mapped[list[ProductSize]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductSize.size"
    )
    photos: Mapped[list[ProductPhoto]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductPhoto.position"
    )

    @property
    def title(self) -> str:
        return f"{self.brand} {self.model}"


class ProductPhoto(Base):
    __tablename__ = "product_photos"
    __table_args__ = (
        CheckConstraint("telegram_file_id IS NOT NULL OR url IS NOT NULL", name="has_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(1024))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="photos")


class ProductSize(Base):
    __tablename__ = "product_sizes"
    __table_args__ = (
        UniqueConstraint("product_id", "size", name="uq_product_sizes_product_size"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= quantity", name="reserved_le_quantity"),
        CheckConstraint("size > 0", name="size_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    size: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped[Product] = relationship(back_populates="sizes")

    @property
    def available(self) -> int:
        return self.quantity - self.reserved


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    telegram_name: Mapped[str | None] = mapped_column(String(128))
    full_name: Mapped[str | None] = mapped_column(String(128))
    contact: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_enum_check("status", OrderStatus), name="status_valid"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        Index("ix_orders_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, Identity(start=1001), nullable=False, unique=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    # Snapshot of client data at the moment of ordering
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    customer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    contact: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OrderStatus.PENDING)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    comment: Mapped[str | None] = mapped_column(Text)
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer | None] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")
    reservations: Mapped[list[StockReservation]] = relationship(back_populates="order")
    history: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderStatusHistory.id"
    )


class OrderItem(Base):
    """Order line. Product data is copied so that later catalog edits never change old orders."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    product_size_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_sizes.id", ondelete="SET NULL"), index=True
    )
    brand: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    line_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="history")


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(_enum_check("status", ReservationStatus), name="status_valid"),
        Index(
            "ix_stock_reservations_active_expires",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_size_id: Mapped[int] = mapped_column(
        ForeignKey("product_sizes.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ReservationStatus.ACTIVE)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="reservations")
    product_size: Mapped[ProductSize] = relationship()


class StockMovement(Base):
    """Append-only journal of every stock change."""

    __tablename__ = "stock_movements"
    __table_args__ = (CheckConstraint(_enum_check("kind", MovementKind), name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_size_id: Mapped[int] = mapped_column(
        ForeignKey("product_sizes.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    delta_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delta_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    comment: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Notification(Base):
    """Outbox of Telegram messages. Rows are written in the same transaction as the business change
    and delivered (with retries) by a background worker, so a Telegram outage never loses an order."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(_enum_check("status", NotificationStatus), name="status_valid"),
        Index(
            "ix_notifications_pending_next",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    keyboard: Mapped[str | None] = mapped_column(String(32))  # symbolic keyboard name, rebuilt at send time
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=NotificationStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(String(512))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WaitlistRequest(Base):
    """Client asked to be notified when a model / size becomes available."""

    __tablename__ = "waitlist_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    size: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    query: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FsmRecord(Base):
    """aiogram FSM state + dialog context, persisted so restarts/redeploys do not break dialogs."""

    __tablename__ = "fsm_records"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
