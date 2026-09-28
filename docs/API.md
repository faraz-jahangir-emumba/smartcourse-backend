# SmartCourse — API Design

**Status:** Module 1. Enrollment, publishing and the assistant arrive in later
modules.

The data model this serves is in [SCHEMA.md](SCHEMA.md). Requirements are in
[PRD.md](PRD.md).

---

## The guiding rule: read big, write small

**Reads are generous.** One call returns a whole course with its modules and
lessons nested. Reading cannot corrupt anything, so make it convenient.

**Writes are narrow.** One call changes one thing. Small requests are easier to
validate, easier to report errors for, and cannot destroy data nobody mentioned.

The asymmetry is deliberate. Convenience matters most where the risk is lowest.

---

## Endpoints

All under `/api/v1`.

### Courses

| Method | Path | Does |
|---|---|---|
| `POST` | `/courses` | Create a course. Starts as `draft` |
| `GET` | `/courses` | List courses. Paginated, filterable by status and instructor |
| `GET` | `/courses/{id}` | One course, with modules and lessons nested |
| `PATCH` | `/courses/{id}` | Edit course fields, and set module order |
| `DELETE` | `/courses/{id}` | Delete a course |

### Modules

| Method | Path | Does |
|---|---|---|
| `POST` | `/courses/{id}/modules` | Add a module. Appended to the end |
| `PATCH` | `/modules/{id}` | Edit its title, and set lesson order |
| `DELETE` | `/modules/{id}` | Delete it, and its lessons and assets |

### Lessons

| Method | Path | Does |
|---|---|---|
| `POST` | `/modules/{id}/lessons` | Add a lesson. Appended to the end |
| `PATCH` | `/lessons/{id}` | Edit its title or content |
| `DELETE` | `/lessons/{id}` | Delete it, and its assets |

### Assets — UC-08

| Method | Path | Does |
|---|---|---|
| `POST` | `/lessons/{id}/assets` | Upload a file. Returns immediately; extraction runs in the background |
| `GET` | `/lessons/{id}/assets` | List files, with their extraction status |
| `DELETE` | `/assets/{id}` | Delete the record and the stored file |

### Users and auth

| Method | Path | Does |
|---|---|---|
| `POST` | `/auth/register` | Create an account |
| `POST` | `/auth/login` | Exchange credentials for a token |
| `GET` | `/users/me` | The current user, including roles |
| `POST` | `/users/me/roles` | Take on an additional role — UC-07 |

---

## Ordering

Order belongs to the **collection**, not to any item in it. "M1 is second" only
means something relative to the others. So the parent owns it.

```
PATCH /courses/{id}
{
  "title": "Advanced React",
  "module_order": ["M2", "M1", "M3"]
}
```

Lessons work the same way, through `PATCH /modules/{id}` with `lesson_order`.

**Send the complete order, never a single move.** "Move M1 to position 2" is
ambiguous — it could mean swap M1 and M2, or shift everything between them.
Those give different results, and the server should not have to guess. Sending
the finished order removes the question.

It is also idempotent: send it twice, get the same result. That property matters
increasingly from Module 2 onwards.

**IDs only, never nested objects.** The request stays small, and there is no
question about what happens to a module that was not included.

Server-side the whole reshuffle happens in one transaction, because positions
are briefly duplicated mid-update. See the reordering section in
[SCHEMA.md](SCHEMA.md).

---

## Three rules that prevent data loss

**1. A missing field never deletes anything.** If `PATCH /courses/{id}` omits a
module, that module is left alone. Removal requires `DELETE`. A truncated
payload or a client bug must never destroy content.

**2. `PATCH` means partial.** Only the fields present are changed. Anything
absent keeps its current value.

**3. Deletion is always explicit**, and always its own request.

---

## Errors

Every non-2xx response has the same shape:

```json
{
  "error": {
    "code": "course_not_found",
    "message": "No course with that id.",
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
  }
}
```

One shape everywhere means a client writes error handling once. `trace_id`
becomes useful in Module 3, when it lets a support question be traced to the
exact request in Jaeger.

| Status | When |
|---|---|
| 400 | The request is malformed |
| 401 | No token, or an invalid one |
| 403 | Authenticated, but not allowed — a student creating a course |
| 404 | No such thing |
| 409 | Conflicts with current state — duplicate enrollment, circular prerequisite |
| 422 | Well-formed but fails validation |

---

## Things that are operations, not fields

Some values look like ordinary columns but are not editable directly.

**`courses.status`** — you do not set a course to `ready`. You publish it, and
the status follows from how that goes. Publishing gets its own endpoint in
Module 2.

**`position`** — the result of a reorder, not a property of a module.

**`assets.extraction_status`** — owned by the background job. Nothing external
sets it.

Worth asking of any field before exposing it: *is this a property of the thing,
or the outcome of something that happened to it?* Outcomes should not be
writable, or callers can put the system into states it could never reach on its
own.

---

## Not here yet

| What | Module |
|---|---|
| Enrollment, progress, completion | 2 |
| Publishing a course | 2 |
| Analytics | 3 |
| The assistant, and streaming answers | 5 |
