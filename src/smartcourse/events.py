"""The five facts SmartCourse publishes, and how each one is written down.

Every function here returns an `OutboxEvent` row, ready to be added to a
session the caller already has open. That is the whole outbox pattern in one
line at the call site:

    session.add(events.student_enrolled(...))

The event and the enrollment then commit together, or neither does. Nothing
here touches Kafka; the dispatcher does that, later, from the table.

Why this module exists at all. The topic, the partition key and the envelope
shape are three things a caller could get wrong silently. Putting them in one
named function per event means no service has to remember any of them, and a
missing field becomes a TypeError at the call site rather than a KeyError in
a consumer next week.

The five cover all nine statistics in FR-19 (PRD section 6):

    total students, total instructors   user.registered, via roles
    total published courses             course.published
    new enrollments over time           student.enrolled
    completion rate                     student.enrolled, course.completed
    average time to finish              the two above, via occurred_at
    most popular courses                student.enrolled less withdrew
    average courses per student         student.enrolled
    failed background jobs              the failed_events table

MODULE-3-PLAN.md also listed `lesson.completed`. It is deliberately not here:
no statistic asks about a single lesson, and `lesson_progress` rows are
already in Postgres for anything that wants them later. An event nobody reads
is retention and schema surface spent on nothing.

See docs/DESIGN.md section 3b.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from smartcourse.infra.db.models.event import OutboxEvent

# One topic per *entity*, holding that entity's whole life story.
#
# Not one topic per event type: `student.enrolled`, `student.withdrew` and
# `course.completed` describe the same enrollment over time, and Kafka
# guarantees order only *within* a topic-partition. Split them up and a
# consumer could see a withdrawal before the enrollment it undoes.
#
# Not one topic for everything either: users have nothing to do with
# enrollments, so a single stream would make every consumer read and discard
# most of what it got, and would fix one retention policy for all of them.
TOPIC_USERS = "smartcourse.users"
TOPIC_COURSES = "smartcourse.courses"
TOPIC_ENROLLMENTS = "smartcourse.enrollments"

# The event types. Strings rather than an Enum because these values leave the
# process - they are written to Postgres, sent over Kafka, and read by code
# that is not this code. The name is the contract; an Enum member would have
# to be converted to that name at every boundary anyway.
USER_REGISTERED = "user.registered"
COURSE_PUBLISHED = "course.published"
STUDENT_ENROLLED = "student.enrolled"
STUDENT_WITHDREW = "student.withdrew"
COURSE_COMPLETED = "course.completed"

# The envelope's shape, not the payload's contents. It changes when a field is
# removed or given a new meaning - never when one is added, because tolerating
# an added field is exactly what consumers are expected to do.
SCHEMA_VERSION = 1

# The longest the dispatcher will wait between retries of one event. Delays
# double - 2s, 4s, 8s - until they hit this ceiling and stop growing.
#
# The cap is what decides recovery time: whatever it is, a recovered service is
# noticed within that long. Thirty minutes is fine for a welcome email.
DEFAULT_BACKOFF_CAP_SECONDS = 1800

# The exceptions. A missing entry gets the default, so adding an event here
# requires remembering nothing, and changing a value is one number.
#
# Note what this is *not* for. None of these events is urgent in the sense of a
# user waiting: Kafka is in no read path at all. A course is `ready` in
# Postgres and fully enrollable before Kafka hears about it, a student is
# enrolled the moment the API returns, and progress is read straight from the
# database. What a delay postpones is a statistic and a notification.
#
# The short cap on course.published is doing a different job. Because any one
# success resets every waiting row (see the dispatcher), the event that retries
# most often acts as a probe for the whole backlog: it knocks on Kafka's door
# every minute, and when Kafka answers, everything drains at once. One fast
# event is enough, which is why this table has three entries and not five.
BACKOFF_CAP_SECONDS = {
    COURSE_PUBLISHED: 60,
    STUDENT_ENROLLED: 300,
    COURSE_COMPLETED: 300,
}


def backoff_cap_seconds(event_type: str) -> int:
    """How long this kind of event may wait between retries, at most.

    Looked up by the dispatcher at send time rather than stored on the row.
    `topic` *is* stored, because routing is a fact about the event decided
    when it is created; a backoff cap is a tuning knob, and a knob stamped
    into four thousand waiting rows cannot be turned during the incident it
    matters in.
    """
    return BACKOFF_CAP_SECONDS.get(event_type, DEFAULT_BACKOFF_CAP_SECONDS)


def _build(
    *,
    topic: str,
    event_type: str,
    partition_key: UUID,
    ordering_key: UUID,
    data: dict[str, Any],
) -> OutboxEvent:
    """Wrap `data` in the standard envelope and return the row to be saved.

    The id is generated here rather than left to the column default, and that
    is the one subtle thing in this file.

    `UUIDMixin` declares `default=uuid.uuid4`, which SQLAlchemy applies while
    building the INSERT - at flush, not at construction. So `OutboxEvent().id`
    is None, and a payload built from it would carry `"event_id": "None"`.
    Nothing would fail: the row saves, the message sends, the consumer runs.
    It would surface only as a consumer unable to tell a redelivery from a new
    event, which is FR-17 broken quietly and found weeks later in wrong
    numbers.

    Deciding the id first and passing it to both places avoids that. Passing
    `id=` explicitly also means the column default never runs.

    `occurred_at` is set here, in Python, and is when the thing *happened*.
    The row's `created_at` is a server default and will be a hair later; they
    answer different questions, and only this one belongs in the message.
    """
    event_id = uuid.uuid4()
    return OutboxEvent(
        id=event_id,
        topic=topic,
        # Kafka keys are bytes, and the producer encodes this on the way out.
        # Stored as a string so the column holds one type across every event,
        # and so the table stays readable in psql.
        partition_key=str(partition_key),
        # Never leaves the database. It is what the dispatcher holds events
        # back on, and is narrower than partition_key for the enrollment
        # events - see the column comment on OutboxEvent.ordering_key.
        ordering_key=str(ordering_key),
        event_type=event_type,
        payload={
            "event_id": str(event_id),
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "version": SCHEMA_VERSION,
            "data": data,
        },
    )


def user_registered(*, user_id: UUID, roles: Sequence[str]) -> OutboxEvent:
    """FR-01. Feeds "total students" and "total instructors".

    `roles` is copied into the event, and it is the only place this module
    copies anything other than an id.

    That is deliberate. Roles can change later (FR-03a), and "Ali registered
    as a student" is a different fact from "Ali is a student today". Looking
    the role up at read time would answer the second question, so a consumer
    replaying this event after a promotion would quietly move Ali from one
    total to the other - rewriting a count of something that already
    happened.
    """
    return _build(
        topic=TOPIC_USERS,
        event_type=USER_REGISTERED,
        partition_key=user_id,
        ordering_key=user_id,
        data={"user_id": str(user_id), "roles": list(roles)},
    )


def course_published(*, course_id: UUID, instructor_id: UUID) -> OutboxEvent:
    """FR-11. Feeds "total published courses".

    Published once per course, by the workflow's final step - not by the
    endpoint that starts it, and not on a republish, which rewrites content
    rather than making the course public. See services/publishing.py, where
    `begin_publish` already works out which of the two is happening.
    """
    return _build(
        topic=TOPIC_COURSES,
        event_type=COURSE_PUBLISHED,
        partition_key=course_id,
        ordering_key=course_id,
        data={"course_id": str(course_id), "instructor_id": str(instructor_id)},
    )


def student_enrolled(
    *, enrollment_id: UUID, student_id: UUID, course_id: UUID
) -> OutboxEvent:
    """FR-12. Feeds four of the nine statistics, and the welcome notification.

    Keyed by `course_id`, not `enrollment_id`. The counters that would break
    under reordering are the per-course ones - a seat count going negative
    because a withdrawal overtook the enrollment it undoes. Sharing a key puts
    every event for one course in one partition, which is the only place Kafka
    promises order.

    All three ids travel, though two could be looked up from the first. They
    are already in hand, they cost nothing, and nearly every statistic groups
    by one of them - so carrying them saves the consumer a query on the common
    path. They are still ids, so this does not break the thin-event rule: no
    titles, no names, nothing that can go stale.
    """
    return _build(
        topic=TOPIC_ENROLLMENTS,
        event_type=STUDENT_ENROLLED,
        partition_key=course_id,
        ordering_key=enrollment_id,
        data={
            "enrollment_id": str(enrollment_id),
            "student_id": str(student_id),
            "course_id": str(course_id),
        },
    )


def student_withdrew(
    *, enrollment_id: UUID, student_id: UUID, course_id: UUID
) -> OutboxEvent:
    """FR-13. Subtracted from "most popular courses"."""
    return _build(
        topic=TOPIC_ENROLLMENTS,
        event_type=STUDENT_WITHDREW,
        partition_key=course_id,
        ordering_key=enrollment_id,
        data={
            "enrollment_id": str(enrollment_id),
            "student_id": str(student_id),
            "course_id": str(course_id),
        },
    )


def course_completed(
    *, enrollment_id: UUID, student_id: UUID, course_id: UUID
) -> OutboxEvent:
    """FR-15. Feeds "completion rate" and "average time to finish a course".

    Both of those are this event measured against the `student.enrolled` for
    the same enrollment - which is why `enrollment_id` travels in all three,
    and is how a consumer pairs them.
    """
    return _build(
        topic=TOPIC_ENROLLMENTS,
        event_type=COURSE_COMPLETED,
        partition_key=course_id,
        ordering_key=enrollment_id,
        data={
            "enrollment_id": str(enrollment_id),
            "student_id": str(student_id),
            "course_id": str(course_id),
        },
    )
