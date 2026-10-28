"""add item variants (parent_item_id, variant_name)

Revision ID: c4e8a1b7d9f3
Revises: a7d3c9e1f5b2
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8a1b7d9f3"
down_revision: str | None = "a7d3c9e1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clothing_items",
        sa.Column("parent_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("clothing_items", sa.Column("variant_name", sa.String(length=100), nullable=True))
    op.create_foreign_key(
        "fk_clothing_items_parent_item_id",
        "clothing_items",
        "clothing_items",
        ["parent_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_clothing_items_parent_item_id", "clothing_items", ["parent_item_id"])


def downgrade() -> None:
    op.drop_index("ix_clothing_items_parent_item_id", table_name="clothing_items")
    op.drop_constraint("fk_clothing_items_parent_item_id", "clothing_items", type_="foreignkey")
    op.drop_column("clothing_items", "variant_name")
    op.drop_column("clothing_items", "parent_item_id")
