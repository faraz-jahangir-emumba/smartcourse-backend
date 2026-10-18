"""drop indexes duplicated by the position constraints

Revision ID: 99b6bc7d6406
Revises: 704fb4a4e3fc
Create Date: 2026-10-18 17:08:10.858872

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '99b6bc7d6406'
down_revision: Union[str, Sequence[str], None] = '704fb4a4e3fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop two indexes that were never used.

    uq_modules_course_position is a unique index on (course_id, position),
    and uq_lessons_module_position is one on (module_id, position). A btree
    index on (a, b) already answers queries filtering on `a` alone, because
    the rows sharing a value of `a` sit next to each other in it.

    So ix_modules_course_id and ix_lessons_module_id duplicated indexes that
    already existed. The planner would never choose them, and every insert
    and update paid to maintain them.

    Read before applying, as every generated migration should be - this one
    is correct, and drops exactly the two indexes and nothing else.
    """
    op.drop_index(op.f('ix_lessons_module_id'), table_name='lessons')
    op.drop_index(op.f('ix_modules_course_id'), table_name='modules')
    # ### end Alembic commands ###


def downgrade() -> None:
    """Put them back. Harmless, just redundant again."""
    op.create_index(op.f('ix_modules_course_id'), 'modules', ['course_id'], unique=False)
    op.create_index(op.f('ix_lessons_module_id'), 'lessons', ['module_id'], unique=False)
    # ### end Alembic commands ###
