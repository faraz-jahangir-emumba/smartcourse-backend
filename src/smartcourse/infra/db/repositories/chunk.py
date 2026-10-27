"""Queries against chunks.

Chunks are derived data - the output of publishing, not something anyone
creates directly - so this repository is shaped around replacing a lesson's
set rather than around inserting one at a time.
"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.content import Chunk
from smartcourse.infra.db.models.course import Lesson, Module


class ChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def existing_for_lesson(self, lesson_id: UUID) -> list[Chunk]:
        """A lesson's current chunks.

        Needed before writing new ones so unchanged content can be left
        alone - see FR-11a and replace_for_lesson in the publishing service.
        """
        rows = await self._session.scalars(
            select(Chunk)
            .where(Chunk.lesson_id == lesson_id)
            .order_by(Chunk.position)
        )
        return list(rows)

    def add(self, chunk: Chunk) -> None:
        self._session.add(chunk)

    async def delete_many(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            await self._session.delete(chunk)

    async def delete_for_lesson(self, lesson_id: UUID) -> None:
        """Remove every chunk for a lesson.

        Used when a lesson's content is emptied: there is nothing to chunk,
        and leaving the old pieces behind would have the assistant quoting
        text the lesson no longer contains.
        """
        await self._session.execute(
            delete(Chunk).where(Chunk.lesson_id == lesson_id)
        )

    async def count_for_course(self, course_id: UUID) -> int:
        """How many chunks a whole course has. For tests and for reporting."""
        rows = await self._session.scalars(
            select(Chunk.id)
            .join(Lesson, Chunk.lesson_id == Lesson.id)
            .join(Module, Lesson.module_id == Module.id)
            .where(Module.course_id == course_id)
        )
        return len(list(rows))

    async def flush(self) -> None:
        await self._session.flush()
