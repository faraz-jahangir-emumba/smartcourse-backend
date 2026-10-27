"""defer the chunk position constraint

Revision ID: ee7e9636791c
Revises: 99b6bc7d6406
Create Date: 2026-10-20 20:26:26.877822

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee7e9636791c'
down_revision: Union[str, Sequence[str], None] = '99b6bc7d6406'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Let a republish reshuffle chunk positions.

    uq_chunks_source_position was created as an ordinary unique constraint,
    checked after every statement. The module and lesson position
    constraints were deliberately made DEFERRABLE INITIALLY DEFERRED because
    reordering needs it; chunks have exactly the same problem and were
    missed.

    A republish recomputes a lesson's chunks and rewrites their positions.
    Inserting a new chunk at position 1 fails while the old one is still
    there - and deleting first does not help, because two chunks that swap
    places collide whichever order the statements run in. There is no
    ordering that works; the check has to move to COMMIT.

    Written by hand. Alembic's autogenerate does not compare deferrability,
    so it produced an empty migration - the same blind spot it has for CHECK
    constraints, and the reason every generated migration here gets read
    before it is applied.

    Raw SQL rather than op.create_unique_constraint because the constraint
    needs both NULLS NOT DISTINCT and DEFERRABLE, and the helper does not
    express the pair. NULLS NOT DISTINCT has to be preserved: chunks taken
    from lessons.content always have a null asset_id, and by default Postgres
    treats every null as different from every other, so without it the
    constraint permits exactly the duplicates it exists to prevent.
    """
    op.execute("ALTER TABLE chunks DROP CONSTRAINT uq_chunks_source_position")
    op.execute(
        """
        ALTER TABLE chunks
            ADD CONSTRAINT uq_chunks_source_position
            UNIQUE NULLS NOT DISTINCT (lesson_id, asset_id, "position")
            DEFERRABLE INITIALLY DEFERRED
        """
    )


def downgrade() -> None:
    """Back to immediate checking. Correct, and it makes republishing a
    lesson whose chunks move fail again."""
    op.execute("ALTER TABLE chunks DROP CONSTRAINT uq_chunks_source_position")
    op.execute(
        """
        ALTER TABLE chunks
            ADD CONSTRAINT uq_chunks_source_position
            UNIQUE NULLS NOT DISTINCT (lesson_id, asset_id, "position")
        """
    )
