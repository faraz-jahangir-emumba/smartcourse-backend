"""The Temporal worker.

Runs beside uvicorn, in its own process:

    uv run python -m smartcourse.worker

Nothing in the API runs workflow code. The API starts workflows; Temporal
tells this process what to run next. Kill it and publishing stops making
progress - it does not fail, it waits, and resumes when a worker returns.

Why it is a separate process rather than a thread inside the API: they
scale differently and fail differently. Publishing a 200-lesson course
should not compete for the event loop that serves logins, and a worker
crash should not take the API with it.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from smartcourse.infra.db.session import dispose_engine
from smartcourse.workflows.activities import ALL_ACTIVITIES
from smartcourse.workflows.publishing import TASK_QUEUE, PublishCourseWorkflow

# Named after this module rather than the root logger. logging.info() goes to
# the root, which means every library's handler configuration applies to it
# and nothing downstream can raise or silence *this* process's logging on its
# own. A named logger is the only one that can be configured separately.
logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)

# Where the server lives. Not in Settings yet because nothing else needs it;
# it moves there the moment the worker runs anywhere but a laptop.
TEMPORAL_ADDRESS = "localhost:7233"


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PublishCourseWorkflow],
        activities=ALL_ACTIVITIES,
    )

    logger.info("worker listening on %s", TASK_QUEUE)
    try:
        await worker.run()
    finally:
        # Return the pooled connections rather than leaving Postgres holding
        # them until it notices the process is gone.
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
