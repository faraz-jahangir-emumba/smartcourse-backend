"""Use cases.

One function per thing a user does - enroll a student, publish a course. Runs
the steps in order, applying domain rules and asking infra to load and save.
Knows nothing about HTTP.

May import from: domain, and infra interfaces.
"""