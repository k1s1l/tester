"""delivery details

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 13:55:30.941397+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("delivery_service", sa.String(length=32), nullable=True))
    op.add_column("customers", sa.Column("city", sa.String(length=80), nullable=True))
    op.add_column("customers", sa.Column("delivery_point", sa.String(length=160), nullable=True))
    op.add_column("orders", sa.Column("delivery_service", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("city", sa.String(length=80), nullable=True))
    op.add_column("orders", sa.Column("delivery_point", sa.String(length=160), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "delivery_point")
    op.drop_column("orders", "city")
    op.drop_column("orders", "delivery_service")
    op.drop_column("customers", "delivery_point")
    op.drop_column("customers", "city")
    op.drop_column("customers", "delivery_service")
