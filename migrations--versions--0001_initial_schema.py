"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-24 16:41:24.559778+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("telegram_name", sa.String(length=128), nullable=True),
        sa.Column("full_name", sa.String(length=128), nullable=True),
        sa.Column("contact", sa.String(length=64), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_customers_telegram_id")),
    )
    op.create_table(
        "fsm_records",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_fsm_records")),
    )
    op.create_index(
        op.f("ix_fsm_records_updated_at"), "fsm_records", ["updated_at"], unique=False
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_products_price_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("sku", name=op.f("uq_products_sku")),
    )
    op.create_index(
        "ix_products_brand_model", "products", ["brand", "model"], unique=False
    )
    op.create_index(
        op.f("ix_products_is_active"), "products", ["is_active"], unique=False
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "number",
            sa.Integer(),
            sa.Identity(always=False, start=1001),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("customer_name", sa.String(length=128), nullable=False),
        sa.Column("contact", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'paid', 'shipped', 'completed', 'rejected', 'cancelled', 'expired')",
            name=op.f("ck_orders_status_valid"),
        ),
        sa.CheckConstraint(
            "total_amount >= 0", name=op.f("ck_orders_total_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_orders_customer_id_customers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_orders_idempotency_key")),
        sa.UniqueConstraint("number", name=op.f("uq_orders_number")),
    )
    op.create_index(
        op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False
    )
    op.create_index(
        "ix_orders_status_created", "orders", ["status", "created_at"], unique=False
    )
    op.create_index(
        op.f("ix_orders_telegram_id"), "orders", ["telegram_id"], unique=False
    )
    op.create_table(
        "product_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=256), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "telegram_file_id IS NOT NULL OR url IS NOT NULL",
            name=op.f("ck_product_photos_has_source"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_photos_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_photos")),
    )
    op.create_index(
        op.f("ix_product_photos_product_id"),
        "product_photos",
        ["product_id"],
        unique=False,
    )
    op.create_table(
        "product_sizes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("size", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity >= 0", name=op.f("ck_product_sizes_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "reserved <= quantity", name=op.f("ck_product_sizes_reserved_le_quantity")
        ),
        sa.CheckConstraint(
            "reserved >= 0", name=op.f("ck_product_sizes_reserved_non_negative")
        ),
        sa.CheckConstraint("size > 0", name=op.f("ck_product_sizes_size_positive")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_sizes_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_sizes")),
        sa.UniqueConstraint("product_id", "size", name="uq_product_sizes_product_size"),
    )
    op.create_index(
        op.f("ix_product_sizes_product_id"),
        "product_sizes",
        ["product_id"],
        unique=False,
    )
    op.create_table(
        "waitlist_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("size", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("query", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_waitlist_requests_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_waitlist_requests")),
    )
    op.create_index(
        op.f("ix_waitlist_requests_product_id"),
        "waitlist_requests",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_waitlist_requests_telegram_id"),
        "waitlist_requests",
        ["telegram_id"],
        unique=False,
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("keyboard", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_notifications_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_notifications_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        op.f("ix_notifications_order_id"), "notifications", ["order_id"], unique=False
    )
    op.create_index(
        "ix_notifications_pending_next",
        "notifications",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_size_id", sa.Integer(), nullable=True),
        sa.Column("brand", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_order_items_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_order_items_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_items_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_order_items_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_size_id"],
            ["product_sizes.id"],
            name=op.f("fk_order_items_product_size_id_product_sizes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(
        op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False
    )
    op.create_index(
        op.f("ix_order_items_product_id"), "order_items", ["product_id"], unique=False
    )
    op.create_index(
        op.f("ix_order_items_product_size_id"),
        "order_items",
        ["product_size_id"],
        unique=False,
    )
    op.create_table(
        "order_status_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_status_history_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_status_history")),
    )
    op.create_index(
        op.f("ix_order_status_history_order_id"),
        "order_status_history",
        ["order_id"],
        unique=False,
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_size_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("delta_quantity", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('initial', 'adjust', 'reserve', 'release', 'expire', 'sale', 'return')",
            name=op.f("ck_stock_movements_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_stock_movements_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_size_id"],
            ["product_sizes.id"],
            name=op.f("fk_stock_movements_product_size_id_product_sizes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_movements")),
    )
    op.create_index(
        op.f("ix_stock_movements_created_at"),
        "stock_movements",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_movements_order_id"),
        "stock_movements",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_movements_product_size_id"),
        "stock_movements",
        ["product_size_id"],
        unique=False,
    )
    op.create_table(
        "stock_reservations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_size_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'released', 'expired')",
            name=op.f("ck_stock_reservations_status_valid"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_stock_reservations_quantity_positive")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_stock_reservations_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_size_id"],
            ["product_sizes.id"],
            name=op.f("fk_stock_reservations_product_size_id_product_sizes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_reservations")),
    )
    op.create_index(
        "ix_stock_reservations_active_expires",
        "stock_reservations",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        op.f("ix_stock_reservations_order_id"),
        "stock_reservations",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_reservations_product_size_id"),
        "stock_reservations",
        ["product_size_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_stock_reservations_product_size_id"), table_name="stock_reservations"
    )
    op.drop_index(
        op.f("ix_stock_reservations_order_id"), table_name="stock_reservations"
    )
    op.drop_index(
        "ix_stock_reservations_active_expires",
        table_name="stock_reservations",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("stock_reservations")
    op.drop_index(
        op.f("ix_stock_movements_product_size_id"), table_name="stock_movements"
    )
    op.drop_index(op.f("ix_stock_movements_order_id"), table_name="stock_movements")
    op.drop_index(op.f("ix_stock_movements_created_at"), table_name="stock_movements")
    op.drop_table("stock_movements")
    op.drop_index(
        op.f("ix_order_status_history_order_id"), table_name="order_status_history"
    )
    op.drop_table("order_status_history")
    op.drop_index(op.f("ix_order_items_product_size_id"), table_name="order_items")
    op.drop_index(op.f("ix_order_items_product_id"), table_name="order_items")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index(
        "ix_notifications_pending_next",
        table_name="notifications",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(op.f("ix_notifications_order_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(
        op.f("ix_waitlist_requests_telegram_id"), table_name="waitlist_requests"
    )
    op.drop_index(
        op.f("ix_waitlist_requests_product_id"), table_name="waitlist_requests"
    )
    op.drop_table("waitlist_requests")
    op.drop_index(op.f("ix_product_sizes_product_id"), table_name="product_sizes")
    op.drop_table("product_sizes")
    op.drop_index(op.f("ix_product_photos_product_id"), table_name="product_photos")
    op.drop_table("product_photos")
    op.drop_index(op.f("ix_orders_telegram_id"), table_name="orders")
    op.drop_index("ix_orders_status_created", table_name="orders")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_products_is_active"), table_name="products")
    op.drop_index("ix_products_brand_model", table_name="products")
    op.drop_table("products")
    op.drop_index(op.f("ix_fsm_records_updated_at"), table_name="fsm_records")
    op.drop_table("fsm_records")
    op.drop_table("customers")
