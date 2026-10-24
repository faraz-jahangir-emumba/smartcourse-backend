"""add full text search columns and indexes

Revision ID: 9c0d602ede75
Revises: ee7e9636791c
Create Date: 2026-10-24 13:55:37.920855

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9c0d602ede75'
down_revision: Union[str, Sequence[str], None] = 'ee7e9636791c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the full-text search columns described in SCHEMA.md.

    They were designed in September and never built - SCHEMA.md described
    them in the present tense, which the Week 1 review caught. Part A §2
    requires "a structure that supports fast retrieval and search", so this
    is a requirement rather than an extra.

    Generated columns: Postgres computes them on every insert and update,
    so no path can leave them stale - not a migration, not a seed script,
    not somebody fixing a title by hand. The alternative, maintaining them
    in application code or a trigger, is a thing that can be forgotten.

    setweight ranks a title match above a body match. GIN rather than btree
    because a tsvector holds many words per row, and "which rows contain
    this word" is what an inverted index answers.

    Generated, and read before applying as every migration here is. Worth
    knowing for later: adding a stored generated column rewrites the table
    and takes an ACCESS EXCLUSIVE lock. Instant on an empty database,
    minutes and an outage on a large one - which is a deployment concern
    this project does not have yet, and would have if it did.
    """
    op.add_column('courses', sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(description, '')), 'B')", persisted=True), nullable=False))
    op.create_index('ix_courses_search', 'courses', ['search_vector'], unique=False, postgresql_using='gin')
    op.add_column('lessons', sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(content, '')), 'B')", persisted=True), nullable=False))
    op.create_index('ix_lessons_search', 'lessons', ['search_vector'], unique=False, postgresql_using='gin')


def downgrade() -> None:
    """Drop them. Search stops working; nothing else notices."""
    op.drop_index('ix_lessons_search', table_name='lessons', postgresql_using='gin')
    op.drop_column('lessons', 'search_vector')
    op.drop_index('ix_courses_search', table_name='courses', postgresql_using='gin')
    op.drop_column('courses', 'search_vector')
