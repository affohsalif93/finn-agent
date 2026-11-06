"""Poller: stateless loop. Mailbox fetch, gate and workflow start follow in a later increment."""

import asyncio
import contextlib

import structlog

from finn.config import Settings
from finn.runtime import connect, install_stop_handlers

log = structlog.get_logger(__name__)


async def run_poller(settings: Settings) -> None:
    stop = install_stop_handlers()
    client = await connect(settings, stop)
    if client is None:
        return
    log.info("poller started", interval_s=settings.poll_interval_seconds)

    while not stop.is_set():
        log.info("poll cycle (no mailbox configured yet)")
        with contextlib.suppress(TimeoutError):  # timeout = next cycle, stop = leave the loop
            await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
    log.info("poller stopped")
