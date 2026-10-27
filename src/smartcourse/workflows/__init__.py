"""Temporal workflows and the activities they orchestrate.

The edge of the application, like `api/`. Everything in here is about
*driving* the use cases in `services/` - none of it contains rules.

The split inside matters:

    activities.py   anything is allowed. Database, network, clock
    publishing.py   orchestration only. Must be deterministic, because
                    Temporal re-runs it from the top to rebuild state

A workflow that reads the clock or queries a database takes a different
path on replay than it did originally, and Temporal detects that as a
non-determinism error. The rule is short: workflows decide, activities do.
"""
