"""product drafts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 19:23:53.713271+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "photo_file_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("existing_product_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'saved', 'skipped')", name=op.f("ck_product_drafts_status_valid")),
        sa.ForeignKeyConstraint(
            ["existing_product_id"],
            ["products.id"],
            name=op.f("fk_product_drafts_existing_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_product_drafts_product_id_products"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_drafts")),
        sa.UniqueConstraint("source_key", name=op.f("uq_product_drafts_source_key")),
    )


def downgrade() -> None:
    op.drop_table("product_drafts")
