"""Course, module and lesson shapes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LessonCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str | None = None


class LessonUpdate(BaseModel):
    """Every field optional - PATCH means "change what I mention".

    A field left out keeps its current value.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = None


class LessonResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # The parent, so a client that arrived by a direct link knows where it
    # is. A real column, so exposing it costs nothing.
    module_id: UUID
    title: str
    content: str | None
    position: int


class LessonDetailResponse(LessonResponse):
    """A lesson fetched on its own.

    Adds course_id, which a lesson row does not carry - the service has it
    from the visibility check, so returning it saves the client a second call
    just to find out which course it is looking at.
    """

    course_id: UUID


class ModuleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class ModuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    # Order belongs to the collection, not to any one item, so a module sets
    # the order of its lessons. Always the complete list - "move this one to
    # position 2" is ambiguous between swapping and shifting.
    lesson_order: list[UUID] | None = None


class ModuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    course_id: UUID
    title: str
    position: int


class ModuleDetailResponse(ModuleResponse):
    """A module with its lessons, for the nested course read."""

    lessons: list[LessonResponse]


class CourseSummary(BaseModel):
    """A course named, not described. Used where one course refers to
    another and the full object would be noise."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str


class PublishAccepted(BaseModel):
    """What POST /courses/{id}/publish returns.

    Not the course. The workflow has been accepted, not finished, and the
    status in the database is still whatever it was a moment ago - handing
    back a course that says "draft" straight after a publish request reads
    like the request was ignored.

    So this says plainly what happened and where to look next.
    """

    course_id: UUID
    # Exposed deliberately. It is the handle for this publish in Temporal's
    # web UI, which is where anyone debugging a stuck publish needs to go.
    workflow_id: str
    status_url: str


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    # Null means unlimited, which is a different thing from 0, which would
    # mean full. Two meanings need two values.
    capacity: int | None = Field(default=None, gt=0)
    # Courses that must be finished first. FR-08.
    #
    # The complete list, every time - the same rule as module_order. "Add
    # this one" would need a matching "remove this one", and then two
    # endpoints can disagree about what the set currently is. Sending the
    # whole set removes the question and makes the call idempotent.
    prerequisite_ids: list[UUID] | None = None


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    capacity: int | None = Field(default=None, gt=0)
    module_order: list[UUID] | None = None
    # Replaces the set. An empty list clears it; omitting the field leaves it
    # alone - which is the difference between "no prerequisites" and "I was
    # not talking about prerequisites".
    prerequisite_ids: list[UUID] | None = None


class CourseResponse(BaseModel):
    """A course on its own - what a listing returns."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instructor_id: UUID
    title: str
    description: str | None
    status: str
    capacity: int | None
    published_at: datetime | None
    created_at: datetime


class CourseDetailResponse(CourseResponse):
    """A course with everything inside it.

    Reads are generous, writes are narrow: one call returns the whole tree,
    because reading cannot corrupt anything. Changing it takes one call per
    thing changed.
    """

    modules: list[ModuleDetailResponse]
    # Titles, not bare ids. A student needs to know what a course requires
    # before being refused by it, and ids alone would mean fetching each one
    # just to render "requires Python Basics".
    #
    # On the detail view only. Putting it on CourseResponse would mean
    # loading prerequisites for every course in a listing, which is a lot of
    # work for something nobody reads until they open one.
    prerequisites: list[CourseSummary] = []


class CourseListResponse(BaseModel):
    """A page of courses.

    The total comes back alongside so a caller knows whether to ask for more.
    It costs a second COUNT query - worth it, because the alternative is
    clients guessing, or fetching everything to find out how much there is.
    """

    items: list[CourseResponse]
    total: int
    limit: int
    offset: int
