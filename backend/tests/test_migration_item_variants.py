import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.migration_helpers import migration_downgrade

PREVIOUS = "a7d3c9e1f5b2"


async def _columns(db: AsyncSession) -> set[str]:
    rows = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'clothing_items'"
        )
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_downgrade_removes_and_upgrade_restores_variant_schema(db_session: AsyncSession):
    assert {"parent_item_id", "variant_name"} <= await _columns(db_session)

    async with migration_downgrade(db_session, PREVIOUS):
        assert not {"parent_item_id", "variant_name"} & await _columns(db_session)

    await db_session.rollback()
    assert {"parent_item_id", "variant_name"} <= await _columns(db_session)
    fk = await db_session.execute(
        text(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conname = 'fk_clothing_items_parent_item_id'"
        )
    )
    assert fk.scalar_one() in ("c", b"c")  # ON DELETE CASCADE
    idx = await db_session.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_clothing_items_parent_item_id'")
    )
    assert idx.scalar_one() == 1
