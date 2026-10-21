"""
One Temporal client per process, created lazily.

Mirrors eap.core.db's engine handling, and for the same reason: production wants a single
cached client, tests want it GONE between tests - each test runs its own event loop, and a
gRPC channel born in a dead loop poisons whichever test runs next. conftest disposes this
exactly where it disposes the engines.
"""

from temporalio.client import Client

from eap.core.config import get_settings

_client: Client | None = None


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(get_settings().temporal_address)
    return _client


async def dispose_temporal_client() -> None:
    """Drop the cached client. It holds no resource needing close(); the loop reclaims it."""
    global _client
    _client = None
