# SmartCourse — Database Schema

**Status:** Module 1 design. Expect changes as later modules land.
**Database:** PostgreSQL 16

This is the shape of the data. Every table and column here exists because
something in [PRD.md](PRD.md) needs it — the "why" column says which.

---

## The tables

```
users
  └─ courses (instructor_id)
       ├─ course_prerequisites (course_id, prerequisite_course_id)
       ├─ modules (course_id)
       │    └─ lessons (module_id)
       │         ├─ assets (lesson_id)
       │         └─ chunks (lesson_id, asset_id)
       └─ enrollments (course_id, student_id)
            └─ lesson_progress (enrollment_id, lesson_id)

failed_events   (stands alone - records failures from every mechanism)
```

Ten tables. The left spine is the content hierarchy — a course holds modules,
which hold lessons, which hold uploaded files and the chunks cut from them. The
right side is what students do with it.

---

## 1. users

Anyone who logs in. One person is one row, whatever they do on the platform.

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | Identity |
| `email` | text | unique, not null | Login, and no two accounts share one |
| `password` | text | not null | Holds a hash, never the real password |
| `full_name` | text | not null | Display |
| `roles` | text[] | not null, default `{student}` | FR-02. A set, not one value — see below |
| `is_active` | boolean | not null, default true | Deactivate instead of deleting |
| `created_at` | timestamptz | not null, default now() | Audit, and "total students" over time |
| `updated_at` | timestamptz | not null, default now() | Audit |

**No foreign keys.** Users depend on nothing.

### Why `roles` is a set

Per PRD assumption A-07, a person can be an instructor *and* a student. An
instructor teaching Python may want to take a statistics course, and making
them keep two accounts is worse than storing a set.

Valid values are `student`, `instructor`, `admin`, enforced with a CHECK so a
typo like `teacher` is rejected by the database rather than merely discouraged
by application code.

Needs a **GIN index** for "find every instructor" to stay fast — array
containment cannot use an ordinary index.

**The alternative was a `user_roles` table.** More conventional, and it leaves
room to record *when* a role was granted and by whom. Rejected for now because
three fixed roles carry no data of their own, and a join on every permission
check buys nothing. If "when did they become an instructor" ever matters, that
is the migration to make.

### Why deleting a user is not really a thing

`is_active` exists because deleting people breaks history. Their enrollments,
completions and the courses they taught all feed analytics that must stay
correct. The foreign keys below enforce this — several of them refuse the
delete outright.

---

## 2. courses

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | |
| `instructor_id` | uuid | → `users.id`, **RESTRICT** | Who owns it |
| `title` | text | not null | |
| `description` | text | | |
| `status` | text | not null, default `draft`, CHECK | The state machine, UC-01 |
| `capacity` | integer | nullable, CHECK > 0 | FR-07. Null means unlimited |
| `published_at` | timestamptz | nullable | When it went live |
| `created_at`, `updated_at` | timestamptz | not null | Audit |

### The status state machine

```
draft ──publish──> publishing ──success──> ready
                        │
                        └───failure────> failed ──retry──> publishing
```

Valid values: `draft`, `publishing`, `ready`, `failed`.

`publishing` exists so a half-processed course is never visible. UC-01 promises
a course is either fully ready or clearly failed, never in between — and that
guarantee needs a state to sit in while the work happens.

The CHECK constraint means an invalid status cannot be written at all, by any
route: the API, a migration, a seed script, or someone fixing data by hand.

### Why `capacity` is nullable

Null means unlimited, which is different from zero (full). Two distinct
meanings need two distinct values, and null is the honest one for "no limit
applies".

---

## 3. course_prerequisites

FR-08: enrollment respects prerequisites. A course can require several others,
and can itself be required by several others. Many-to-many, so it needs a table
of its own.

| Column | Type | Constraints |
|---|---|---|
| `course_id` | uuid | → `courses.id`, **CASCADE** |
| `prerequisite_course_id` | uuid | → `courses.id`, **RESTRICT** |

Primary key is both columns together, which also stops the same prerequisite
being added twice.

Add `CHECK (course_id <> prerequisite_course_id)` or a course can require
itself, making it permanently unenrollable.

**Two foreign keys to the same table, two different rules.** Deleting a course
should remove its own list of requirements (CASCADE), but must not be allowed
while other courses depend on it (RESTRICT) — that would silently change their
entry rules.

### Cycles: one hop in the database, longer chains in code

`CHECK (course_id <> prerequisite_course_id)` catches a course requiring itself,
because everything needed to judge that row is in the row.

Longer cycles are not visible to a constraint. A requires B, B requires C, C
requires A is spread across three rows, and each one is perfectly ordinary on
its own. Detecting it means walking the chain, which a CHECK cannot do — it
sees one row at a time.

This is still **enforced**, just one layer up. When a prerequisite is added, the
service asks whether the target course is already reachable by following the
new prerequisite's own chain. If it is, the link would close a loop and is
rejected. Postgres answers that in a single `WITH RECURSIVE` query.

A trigger could do the same check inside the database, which would hold no
matter what writes to it. Rejected: the logic would then live in SQL, where it
is harder to test and invisible to anyone reading the Python.

Why it matters: a cycle makes every course in it permanently unenrollable, and
does so silently. No error, just students who can never get in.

See FR-08a.

---

## 4. modules

A chapter within a course.

| Column | Type | Constraints |
|---|---|---|
| `id` | uuid | primary key |
| `course_id` | uuid | → `courses.id`, **CASCADE** |
| `title` | text | not null |
| `position` | integer | not null |
| `created_at`, `updated_at` | timestamptz | not null |

**UNIQUE (course_id, position) DEFERRABLE INITIALLY DEFERRED** — two modules
cannot both be third. See [Reordering](#reordering) for why it is deferrable.

### Why `position` exists

A course is a sequence, not a bag. A database returns rows in no guaranteed
order unless asked, and that order can change between queries. Without an
explicit position the intended order is simply not stored anywhere, and cannot
be recovered later.

---

## 5. lessons

| Column | Type | Constraints |
|---|---|---|
| `id` | uuid | primary key |
| `module_id` | uuid | → `modules.id`, **CASCADE** |
| `title` | text | not null |
| `content` | text | | The teaching text |
| `position` | integer | not null |
| `created_at`, `updated_at` | timestamptz | not null |

**UNIQUE (module_id, position) DEFERRABLE INITIALLY DEFERRED**

`content` is where Part A meets Part B. It is what UC-01 splits into chunks,
and what the assistant searches in UC-05.

---

## 6. assets

Files uploaded against a lesson. UC-08.

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | |
| `lesson_id` | uuid | → `lessons.id`, **CASCADE** | What it belongs to |
| `filename` | text | not null | What the instructor called it |
| `content_type` | text | not null | `application/pdf`, `video/mp4` — decides the extractor |
| `size_bytes` | bigint | not null | Limits and display |
| `storage_path` | text | not null | Where the bytes actually are |
| `extraction_status` | text | not null, default `pending`, CHECK | Progress of the background job |
| `extracted_text` | text | nullable | The result, once there is one |
| `extraction_error` | text | nullable | Why it failed, for retry |
| `created_at`, `updated_at` | timestamptz | not null | |

### storage_path

The table records where the file is, not the file itself. `storage_path` is a
pointer, and what it points into is **not decided yet** — a folder on disk,
object storage, something else.

Deliberately left open. Nothing else in the schema depends on the answer: the
column holds a string either way, and only the code that reads and writes the
bytes changes. Decided before assets are implemented in Module 2.

### extraction_status

`pending` · `extracting` · `done` · `failed` · `not_applicable`

This column is what makes the background work manageable. Module 2 needs to
find everything still pending, retry what failed, and skip what has no text to
give. `not_applicable` covers images and anything else without text — a
distinct state from failure, because nothing went wrong.

Which formats actually get extracted is PRD assumption A-08. The design does
not change per format; only the extractor does.

---

## 7. chunks

Lesson material cut into retrievable pieces. Part A, Core Functional
Requirements §2 names these explicitly — *"broken into components (modules,
lessons, chunks)"* — and §4 lists *"preparing course material for intelligent
Q&A"* as a Part A background task. They are produced here and consumed by
Part B.

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | |
| `lesson_id` | uuid | → `lessons.id`, **CASCADE** | Always known, so answers can cite a lesson (FR-22) |
| `asset_id` | uuid | → `assets.id`, **CASCADE**, nullable | Which file it came from. Null means it came from `lessons.content` |
| `content` | text | not null | The chunk itself |
| `position` | integer | not null | Order within its source, so neighbours can be found |
| `token_count` | integer | not null | Budgeting what fits in a prompt |
| `content_hash` | text | not null | Skip re-embedding text that has not changed |
| `created_at` | timestamptz | not null | |

**UNIQUE (lesson_id, asset_id, position)**

Index on `content_hash` — it is looked up on every republish.

### Why chunks rather than whole lessons

Two limits force it.

An LLM can only read so much at once. A course may hold 200 lessons; the
assistant must be given only the few passages likely to answer the question. The
content has to already be in passage-sized pieces for that to be possible.

And embeddings blur as text grows. An embedding represents one piece of text as
one point in meaning-space. That is sharp when the text covers one idea, and
useless when it averages five — the result sits slightly near everything and
close to nothing.

Chunk size is the trade-off between those: small enough to mean one thing, large
enough to still make sense alone. Around 300–500 tokens, with 10–15% overlap so
an idea spanning a boundary survives in at least one piece.

Tokens rather than characters because that is the unit models count. Counting
them needs a tokenizer library at chunking time.

### Why `lesson_id` is never null

A chunk from a PDF still belongs to a lesson. Keeping the link direct means
citation never has to walk back through the asset, and a chunk is never
orphaned from the thing a student can actually open.

### `content_hash` and republishing

Editing one lesson and republishing must not re-embed the whole course —
embedding costs time and money in proportion to volume.

So publishing recomputes chunks, hashes each one, and compares. Unchanged
hashes keep their existing embeddings; only new or changed chunks are
re-embedded. A cheap column now that saves the expensive work later.

---

## 8. failed_events

Part A §5 lists **"Failed Events / Workflow Issues"** among the nine metrics, so
failures need somewhere to live rather than only a log line.

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | |
| `source` | text | not null, CHECK | `kafka` / `celery` / `temporal` |
| `source_name` | text | not null | Topic, task or workflow name |
| `payload` | jsonb | not null | What was being processed, so it can be retried |
| `error` | text | not null | What went wrong |
| `retry_count` | integer | not null, default 0 | How many attempts so far |
| `status` | text | not null, default `pending`, CHECK | `pending` / `retrying` / `resolved` / `abandoned` |
| `occurred_at` | timestamptz | not null, default now() | |
| `resolved_at` | timestamptz | nullable | |

Detailed in Module 3, when Kafka, Celery and Temporal arrive and there is
something to fail. Recorded here so the metric has a source and the schema
does not have to change later.

`payload` is `jsonb` rather than text because the three sources carry different
shapes, and jsonb can still be queried.

---

## 9. enrollments

| Column | Type | Constraints | Why |
|---|---|---|---|
| `id` | uuid | primary key | |
| `student_id` | uuid | → `users.id`, **RESTRICT** | Who |
| `course_id` | uuid | → `courses.id`, **RESTRICT** | What |
| `status` | text | not null, default `active`, CHECK | `active` / `completed` / `withdrawn` |
| `enrolled_at` | timestamptz | not null, default now() | Start of the clock |
| `completed_at` | timestamptz | nullable | End of the clock |

### The conflict between FR-06 and FR-10, and how it is solved

FR-06 says a student cannot enroll in the same course twice. The obvious answer
is `UNIQUE (student_id, course_id)`.

But FR-10 promises enrollment **history** — so a student who withdraws and later
returns needs a second row. A plain unique constraint makes that impossible.

Postgres solves it with a **partial unique index**: unique only across rows that
match a condition.

```sql
CREATE UNIQUE INDEX one_active_enrollment
  ON enrollments (student_id, course_id)
  WHERE status = 'active';
```

Many enrollments per student and course are now allowed, but only **one active
at a time**. Both requirements are satisfied, and enforced by the database
rather than trusted to application code.

### Why both keys RESTRICT

Deleting a student or a course would erase learning history that the analytics
in FR-19 depend on. Neither is deletable while enrollments exist. Deactivate
instead.

### Why `completed_at` is a column

"Average time to complete a course" is one of the nine statistics. It is
`completed_at` minus `enrolled_at`. Neither can be recovered later if not
recorded at the time.

---

## 10. lesson_progress

One row each time a student finishes a lesson. UC-03.

| Column | Type | Constraints |
|---|---|---|
| `id` | uuid | primary key |
| `enrollment_id` | uuid | → `enrollments.id`, **CASCADE** |
| `lesson_id` | uuid | → `lessons.id`, **CASCADE** |
| `completed_at` | timestamptz | not null, default now() |

**UNIQUE (enrollment_id, lesson_id)** — a lesson is completed once per
enrollment. Without this, clicking twice creates two rows and percentages
exceed 100.

### Why it points at the enrollment, not the student

This is what makes re-enrollment work. A second attempt at a course is a
different enrollment, so it starts with fresh progress rather than inheriting
the first attempt's.

### Three numbers, one table

The PRD asks for overall course progress, which modules are complete, and how
far through the current module a student is. None of those are stored.

- **Course %** — completed lessons ÷ total lessons in the course
- **Module complete?** — every lesson in that module has a row
- **Current module %** — completed lessons in it ÷ lessons in it

**Store facts, calculate summaries.** A stored percentage goes stale the moment
an instructor adds a lesson, silently, everywhere, until something recalculates
it. "This person finished that lesson at that time" stays true forever.

Calculating costs more than reading a number. At 200 lessons per course that is
nothing. If it ever becomes slow, a cached summary can be added — but because a
measurement said so, not in advance.

---

## Delete behaviour, all together

Each rule describes what happens **when the row being pointed at is deleted** —
the parent, not the row holding the key.

| Foreign key | On delete of | Behaviour | Why |
|---|---|---|---|
| `courses.instructor_id` | a user | **RESTRICT** | Do not destroy courses by removing a person |
| `course_prerequisites.course_id` | a course | **CASCADE** | Its requirement list belongs to it |
| `course_prerequisites.prerequisite_course_id` | a course | **RESTRICT** | Other courses depend on it |
| `modules.course_id` | a course | **CASCADE** | A module alone is unreachable |
| `lessons.module_id` | a module | **CASCADE** | Same |
| `assets.lesson_id` | a lesson | **CASCADE** | Same |
| `chunks.lesson_id` | a lesson | **CASCADE** | Chunks are derived from it |
| `chunks.asset_id` | an asset | **CASCADE** | Same |
| `enrollments.student_id` | a user | **RESTRICT** | History has value |
| `enrollments.course_id` | a course | **RESTRICT** | History has value |
| `lesson_progress.enrollment_id` | an enrollment | **CASCADE** | Belongs to it |
| `lesson_progress.lesson_id` | a lesson | **CASCADE** | Meaningless without the lesson |

**The pattern:** the content hierarchy cascades, because a child cannot exist
without its parent. Anything touching people or history restricts, because that
data outlives whatever created it.

`SET NULL` is used nowhere — nothing in this design is meaningful while
orphaned.

---

## Search

Part A §2 requires content stored in a structure supporting *"Fast retrieval"*
and *"Search"*.

Worth separating two things the brief keeps apart. Part A asks for **search**;
Part B's version of the same section asks for *"context-based queries"*. They
are different problems:

- **Search (Part A)** — the words the student typed. Postgres does this itself.
- **Semantic search (Part B)** — meaning rather than words, via embeddings over
  the `chunks` table. Module 4.

Postgres full-text search covers the Part A half with no extra service:

```sql
-- on lessons
search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')),   'A') ||
    setweight(to_tsvector('english', coalesce(content, '')), 'B')
) STORED
```

plus a GIN index on it. The same on `courses` over title and description, for
FR-05.

**Why a generated column** rather than filling it in application code: Postgres
maintains it on every insert and update, automatically. There is no path — a
migration, a seed script, a manual fix — that can leave it stale, and no trigger
to write or test.

**`setweight`** ranks a title match above a body match, so a lesson *called*
"Recursion" beats one that mentions the word in passing.

**`'english'`** applies English stemming, so "running" matches "run". It also
means the language is a decision baked into the column — noted in known gaps.

---

## Reordering

`modules.position` and `lessons.position` are unique within their parent, so a
naive reorder fails. Modules at 1, 2, 3; move the third to the front; the first
`UPDATE` collides with the module already holding position 1. Postgres checks
after **every statement**, so it rejects the change halfway even when the final
state would have been valid.

Two ways out. Dropping the constraint is the obvious one, and it is wrong — two
modules could then both sit at position 3, the order becomes whatever the
database feels like returning, and it fails silently.

Instead the constraint is **`DEFERRABLE INITIALLY DEFERRED`**: checked once at
commit rather than after each statement. Positions may be duplicated in the
middle of a transaction as long as they are correct by the end. The rule
becomes "no duplicates when the work is finished", which is what was always
meant.

**This puts a requirement on the service layer: every position change for a
course happens inside one transaction.** An endpoint that updates a single
module's position on its own will fail, and should.

The API takes the **whole order** rather than "move this one to position 2" —
see [API.md](API.md). That removes the ambiguity between swapping two items and
shifting a run of them, and makes the operation idempotent.

Sparse positions (100, 200, 300, inserting at 150) would avoid the problem
differently. Rejected: positions stop being readable, and the gaps eventually
run out and need renumbering anyway.

---

## Conventions

- **UUID primary keys**, generated in the application rather than by the
  database. This matters later: the outbox pattern in Module 3 needs an id
  before the row is written, and a workflow needs to refer to something it has
  not yet inserted. Sequential integers cannot do that.
- **`timestamptz`, never `timestamp`.** Stores the instant with its offset.
  Plain `timestamp` silently loses the zone and produces wrong answers the
  moment anything crosses one.
- **Plural, snake_case table names.** A convention, not a rule — the value is
  in being consistent.
- **`created_at` / `updated_at` everywhere.** Cheap to add now, impossible to
  backfill honestly later.
- **CHECK constraints on every fixed set of values.** The application is not
  the only thing that writes to this database.

---

## Known gaps

| Gap | Plan |
|---|---|
| Circular prerequisite chains beyond one hop | Enforced in the service layer with a recursive query, not by a constraint. FR-08a |
| Two instructors editing one course at once | Last write wins today. If it becomes a real problem, add a `version` column to `courses` and reject writes carrying a stale version |
| Full-text search is English-only | `to_tsvector('english', ...)` is fixed in the generated column. Multi-language would need a language column per row and a different index strategy |
| Chunk size and overlap are not yet fixed | Decided in Module 2 when publishing is built, and it must be right — Module 4's embeddings are built on whatever it produces |
| Embeddings for chunks | Module 4. A vector column is added to `chunks`; nothing else about the table changes |
| Content chunks for the assistant | Module 4. Depends on the chunking decisions made in Module 2 |
| Outbox table for reliable events | Module 3, when Kafka arrives |
| Whether an admin may also be a student | Currently allowed. Restrict it with a CHECK if that turns out to be wrong |
