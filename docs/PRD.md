# SmartCourse — Product Requirements Document

**Written by:** Faraz Jahangir
**Date:** 2 September 2026
**Status:** Living document. Expect it to change as I learn.
**Build window:** 2 September – 31 October 2026

---

## 1. What this is

EduCorp runs an online learning platform. Universities, companies and training
academies use it to teach people. Instructors put courses on it. Students take
those courses.

The platform works, but it was built for a smaller number of users. Now that
more people are using it, it is starting to crack. EduCorp has asked for a new
backend. This document says what that backend has to do.

---

## 2. The problem

Five things are going wrong today.

**Publishing a course is slow and manual.** When an instructor wants to launch
a new course or update an old one, too much of the work happens by hand. So
courses go up slowly, and instructors avoid making small improvements because
each one is a chore.

**Students cannot find things.** There is no smart search. If a student has a
question about the course material, there is nothing to ask. They scroll and
guess.

**The data does not agree with itself.** Course information, student progress
and the reporting dashboards are kept in different places. When they drift
apart, the dashboard shows one number and the platform shows another. Nobody
knows which to trust.

**Busy periods cause delays.** When a lot of people enroll at once — a course
launch, or a company putting a whole department through training — enrollments,
emails and background jobs pile up and everything slows down.

**Useful data is thrown away.** Every time a student reads a lesson, asks a
question or finishes a module, that tells you something. Right now none of it
is used to help anyone learn better.

### Why this is happening now

Growth. The old design assumed one job could finish before the next one
started. That holds when a hundred people use the platform. It stops holding
when thousands do, especially when they all arrive at the same time.

This is why the fix is not "make the code faster". The fix is a different
shape: work that does not need to happen right now should happen in the
background, and the system should keep working when one part of it fails.

### What success looks like

An instructor publishes a course and it goes live on its own, reliably, without
anyone watching it. A student enrolls instantly even during a rush. A student
can ask a question about the material and get a real answer. And when something
does break, we can find out what and why in minutes.

---

## 3. Who uses it

| Role | What they do |
|---|---|
| **Student** | Browses courses, enrolls, works through lessons, tracks their progress, asks the assistant questions about the material |
| **Instructor** | Creates courses, adds modules and lessons, publishes and updates them, generates summaries and quizzes from their own material |
| **Admin** | Manages users and their roles, looks at platform-wide numbers, checks on failed background jobs |

---

## 4. Scope

### In scope — Part A (the core platform)

- Accounts, login, and the three roles above
- Creating and editing courses, modules and lessons
- Publishing a course, including all the background work that follows
- Enrolling students, with rules about duplicates, limits and prerequisites
- Tracking progress and completion
- Background jobs that run reliably and can recover from failure
- Platform statistics
- Being able to see inside the system when something goes wrong

### In scope — Part B (the AI layer)

- A student can ask questions about a course and get answers based on that
  course's real content
- An instructor can generate summaries, learning objectives and quiz questions
  from their own material
- Long answers appear gradually as they are written, instead of after a long wait
- Sensible behaviour when a question is vague or off-topic
- Statistics on how the assistant is being used

### Out of scope

These are things a real learning platform would have. I am not building them,
on purpose, because they would eat time without teaching me anything the
assignment is asking about.

- Any user interface. This is a backend only.
- Payments, pricing, refunds
- Video hosting and streaming
- Mobile apps
- Live classes, chat between students, forums
- Deploying to real servers. Everything runs locally in Docker.

---

## 5. Use cases

### UC-01 — An instructor publishes a course

**Before:** The course exists as a draft and has at least one module with at
least one lesson.

**What happens:**
1. The instructor asks to publish.
2. The system checks the course is complete enough to publish.
3. The course is marked as "publishing" so nobody sees a half-finished version.
4. In the background, the text of each lesson is pulled out and cut into small
   pieces. (These pieces are what the AI assistant will search through later.)
5. The pieces are saved.
6. Once everything has finished, the course is marked "ready" and appears in
   the catalog.
7. The rest of the system is told the course was published, so statistics and
   notifications can update.

**If something fails:** The system tries the failed step again a few times. If
it still fails, everything that was already done is undone and the course goes
to a "failed" state. The important rule: a course is never left half-published.

**After:** The course is either fully ready, or clearly failed. Never in between.

---

### UC-02 — A student enrolls in a course

**Before:** The student is logged in. The course is published and has space.

**What happens:**
1. The student asks to enroll.
2. The system checks they are not already enrolled, that the course has not hit
   its limit, and that they have finished any prerequisite courses.
3. The enrollment is recorded.
4. A progress record is created, starting at zero.
5. The rest of the system is told, so statistics update and a welcome email goes out.

**If something fails:** If the student clicks twice, or their connection drops
and they retry, they still end up with exactly one enrollment. Not two.

**After:** The student is enrolled once, can see the course, and their progress
starts at zero.

---

### UC-03 — A student works through a course

The student opens lessons and marks them complete. Their progress goes up. When
they finish the last lesson, the course counts as complete and their completion
is recorded.

---

### UC-04 — An admin looks at the numbers

The admin opens the statistics and sees how many students and instructors there
are, how many courses are published, how enrollments are trending, how many
people finish what they start, which courses are most popular, and how many
background jobs have failed.

---

### UC-05 — A student asks the assistant a question

**Before:** The student is enrolled and the course is published.

**What happens:**
1. The student asks something in plain language.
2. The system finds the parts of the course most likely to answer it.
3. Those parts are given to the AI along with the question.
4. The answer comes back gradually, word by word, rather than all at once after
   a long pause.
5. The answer says which lessons it came from.

**If the question is vague or not about the course:** The assistant says so
instead of making something up.

---

### UC-06 — An instructor generates teaching material

The instructor picks a module and asks for a summary, a list of learning
objectives, or some quiz questions. The system generates them from that
module's actual content. Because this can take a while, the instructor is not
left staring at a frozen screen.

---

## 6. Functional requirements

What the system must do. Each one has an ID so I can point at it later.

| ID | Requirement | Part |
|---|---|---|
| FR-01 | People can register and log in | A |
| FR-02 | Every user is a student, an instructor, or an admin | A |
| FR-03 | Users can only do what their role allows | A |
| FR-04 | Instructors can create and edit courses, modules and lessons | A |
| FR-05 | Students can browse and search published courses | A |
| FR-06 | A student cannot enroll in the same course twice | A |
| FR-07 | Enrollment respects course capacity limits | A |
| FR-08 | Enrollment respects prerequisite courses | A |
| FR-09 | Enrolling creates a progress record | A |
| FR-10 | Past enrollments are kept as history | A |
| FR-11 | Publishing splits lesson content into searchable pieces | A |
| FR-12 | A course is only marked ready when all its background work finishes | A |
| FR-13 | A failed publish never leaves a course half-published | A |
| FR-14 | Students can mark lessons complete and see their progress | A |
| FR-15 | Finishing every lesson completes the course | A |
| FR-16 | Background work happens outside the request, so the user is not kept waiting | A |
| FR-17 | Background work that runs twice does not double-count anything | A |
| FR-18 | Failed background work is kept, not silently dropped | A |
| FR-19 | The nine platform statistics can be read | A |
| FR-20 | A failure in publishing, enrolling or a background job can be traced to its cause | A |
| FR-21 | Students can ask questions and get answers from the real course content | B |
| FR-22 | Answers say which lessons they came from | B |
| FR-23 | Instructors can generate summaries, objectives and quiz questions | B |
| FR-24 | Long answers stream out gradually | B |
| FR-25 | Vague or off-topic questions get an honest answer, not an invented one | B |
| FR-26 | Assistant usage is recorded | B |

### The nine statistics (FR-19)

Total students · total instructors · total published courses · new enrollments
over time · completion rate · average time to finish a course · most popular
courses · average courses per student · failed background jobs.

---

## 7. Non-functional requirements

Not what it does — how well it does it.

> **These numbers are mine to justify.** The brief only says "must be fast" and
> "must scale", which is not something you can test. Picking real numbers and
> being able to explain them is treated as a strength. The ones marked TBD are
> ones I want to measure before committing to.

| ID | Requirement | Target |
|---|---|---|
| NFR-01 | Reading a course or catalog page, 95% of the time | TBD ms |
| NFR-02 | Creating or updating something, 95% of the time | TBD ms |
| NFR-03 | Enrollments handled per second without losing any | TBD /sec |
| NFR-04 | A typical course finishes publishing within | TBD |
| NFR-05 | The assistant starts replying within | 2 seconds |
| NFR-06 | Events that get lost | Zero. Everything is either handled or kept for inspection |
| NFR-07 | Statistics after a full rebuild | Identical to before |
| NFR-08 | Test coverage on business logic | 70% or more |
| NFR-09 | Any flow that crosses components | Traceable end to end |
| NFR-10 | The whole system runs on one developer laptop | 16 GB RAM |

---

## 8. Assumptions

The brief leaves gaps on purpose. Here is where I filled one in, and why.

| # | What the brief does not say | What I decided | Why |
|---|---|---|---|
| A-01 | How login works | Token-based login. Tokens last an hour. No "remember me" or password reset. | Enough to prove roles work. A full login system would cost a week and teach me nothing this assignment is about. |
| A-02 | Whether multiple organisations share the platform | One organisation only | Nothing in the brief needs separation between companies. |
| A-03 | What a certificate is | A record that a student finished, plus an event. No PDF. | Certificates are mentioned once, with no rules. The interesting part is the completion workflow, not the file. |
| A-04 | How students are notified | Email only, captured locally so nothing real is sent | Adding SMS or push would be more of the same work, not new learning. |
| A-05 | What "fast" means | See the numbers in section 7 | Something testable is better than something vague. |
| A-06 | How big a course is | Up to 20 modules, 200 lessons, 500 words per lesson | I need a size to design chunking and indexing around. |

---

## 9. Milestones

| Week | Dates | What gets built |
|---|---|---|
| 1 | 2–7 Sep | This document, system design, decision records, everything running locally |
| 2–3 | 8–21 Sep | **Module 1** — accounts, roles, courses, modules, lessons, working APIs |
| 4–5 | 22 Sep – 5 Oct | **Module 2** — enrollment, and the publishing workflow |
| 6–7 | 6–19 Oct | **Module 3** — background events, statistics, and seeing inside the system |
| 8 | 20–26 Oct | **Module 4** — searching course content by meaning |
| 9 | 27–31 Oct | **Module 5** — the assistant, streaming answers, final write-up |

---

## 10. Traceability

One row per requirement. It shows where each one was designed, built and
tested. Filled in as I go — not at the end.

| Requirement | Design | Code | Test | Week |
|---|---|---|---|---|
| FR-01 to FR-03 | | | | 2 |
| FR-04, FR-05 | | | | 3 |
| FR-06 to FR-10 | | | | 4 |
| FR-11 to FR-13 | | | | 5 |
| FR-14, FR-15 | | | | 4 |
| FR-16 to FR-18 | | | | 6 |
| FR-19, FR-20 | | | | 7 |
| FR-21, FR-22 | | | | 8 |
| FR-23 to FR-26 | | | | 9 |
