"""Shared fixtures."""

from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.testing import WorkflowEnvironment


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """One local Temporal dev server (in-memory SQLite) for the whole test session.

    The first run downloads the Temporal CLI into the temp dir and reuses it afterwards.
    """
    async with await WorkflowEnvironment.start_local() as env:
        yield env
