"""Connecting to Temporal.

Infrastructure, like session.py. The API needs a client to *start* workflows;
it never runs workflow code, which is the worker's job.
"""

import logging

from temporalio.client import Client

logger = logging.getLogger(__name__)

# Where the server lives. Not in Settings yet because only two places need
# it and both are local; it moves there the moment anything runs elsewhere.
TEMPORAL_ADDRESS = "localhost:7233"

_client: Client | None = None


async def get_temporal_client() -> Client:
    """The shared client, connected on first use.

    One connection for the process, not one per request. Client.connect
    opens a gRPC channel and does a handshake - cheap compared with a
    database connection, and still not something to do on every publish.

    Connected lazily rather than at startup, so the API comes up even when
    Temporal is not running. The alternative would mean a developer who only
    wants to look at /docs cannot start the app without a workflow engine.
    The cost is that a down Temporal is discovered by the first publish
    rather than at boot - and that failure is reported honestly, see
    api/errors.py.
    """
    global _client
    if _client is None:
        logger.info("connecting to Temporal at %s", TEMPORAL_ADDRESS)
        _client = await Client.connect(TEMPORAL_ADDRESS)
    return _client


async def reset_temporal_client() -> None:
    """Forget the cached client. For tests, and for a clean shutdown."""
    global _client
    _client = None
