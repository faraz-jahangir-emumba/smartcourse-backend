"""Uploaded files and the chunks cut from lesson material.

See docs/SCHEMA.md sections 6 and 7.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smartcourse.infra.db.base import Base, TimestampMixin, UUIDMixin

# Where an upload is in the extraction pipeline.
#
# not_applicable is deliberately distinct from failed. An image has no text to
# extract, which is not the same as extraction having gone wrong, and the two
# need different handling: one is finished, the other should be retried.
EXTRACTION_STATUSES = ("pending", "extracting", "done", "failed", "not_applicable")


class Asset(UUIDMixin, TimestampMixin, Base):
    """A file uploaded against a lesson. UC-08.

    The bytes are not here - only a pointer to them. Where that pointer leads
    is still undecided (PRD A-09); nothing in this table depends on the answer.
    """

    __tablename__ = "assets"

    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lessons.id", ondelete="CASCADE"),
        index=True,
    )

    filename: Mapped[str] = mapped_column(String(500))

    # Decides which extractor runs: application/pdf, video/mp4, image/png.
    content_type: Mapped[str] = mapped_column(String(200))

    # BigInteger, not Integer. A plain integer stops at about 2GB, which a
    # video would pass.
    size_bytes: Mapped[int] = mapped_column(BigInteger)

    storage_path: Mapped[str] = mapped_column(String(1000))

    extraction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", index=True
    )

    # Filled in once extraction succeeds. Null until then, and for files that
    # have no text to give.
    extracted_text: Mapped[str | None] = mapped_column(Text)

    # Why it failed, kept so a failure can be understood and retried rather
    # than just observed.
    extraction_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "extraction_status IN "
            "('pending', 'extracting', 'done', 'failed', 'not_applicable')",
            name="ck_assets_extraction_status_valid",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_assets_size_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Asset {self.filename!r} {self.extraction_status}>"


class Chunk(UUIDMixin, Base):
    """A retrievable piece of lesson material.

    Part A names chunks explicitly (Core Functional Requirements section 2),
    and lists preparing material for Q&A as a Part A task in section 4. They
    are produced during publishing in Module 2 and embedded in Module 4.

    Two limits force chunks to exist. An LLM can only read so much at once, so
    the assistant must be handed the few passages likely to answer a question -
    which requires the content to already be in passages. And an embedding
    blurs as text grows: it represents one piece of text as one point in
    meaning-space, which is sharp for one idea and useless for five.

    No TimestampMixin. A chunk is derived data, replaced wholesale when its
    source changes, so "when was this last updated" says nothing useful.
    """

    __tablename__ = "chunks"

    # Never null, even for a chunk that came from a PDF. FR-22 requires answers
    # to cite a lesson, and keeping the link direct means citation never has to
    # walk back through the asset.
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lessons.id", ondelete="CASCADE"),
        index=True,
    )

    # Null means the chunk came from lessons.content rather than a file.
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        index=True,
    )

    content: Mapped[str] = mapped_column(Text)

    # Order within its source, so neighbouring chunks can be found when a
    # retrieved passage needs its surrounding context.
    position: Mapped[int] = mapped_column(Integer)

    # Tokens, not characters - tokens are what a model counts, and what decides
    # how much fits in a prompt. Counting them needs a tokenizer at chunk time.
    token_count: Mapped[int] = mapped_column(Integer)

    # Lets a republish skip work. Editing one lesson must not re-embed the
    # other 199: hash each chunk, compare, and only re-embed what changed.
    # Embedding costs time and money in proportion to volume. FR-11a.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    __table_args__ = (
        # postgresql_nulls_not_distinct matters here and is easy to miss.
        #
        # By default Postgres treats every NULL as different from every other
        # NULL, so two rows with asset_id NULL and the same lesson and position
        # would BOTH be allowed - exactly the duplicate this constraint exists
        # to prevent, and chunks from lessons.content always have a null
        # asset_id. Postgres 15+ can be told to treat nulls as equal.
        UniqueConstraint(
            "lesson_id",
            "asset_id",
            "position",
            name="uq_chunks_source_position",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("position >= 0", name="ck_chunks_position_non_negative"),
        CheckConstraint("token_count > 0", name="ck_chunks_token_count_positive"),
    )

    def __repr__(self) -> str:
        return f"<Chunk lesson={self.lesson_id} pos={self.position} tokens={self.token_count}>"
