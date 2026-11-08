"""Shared process plumbing: clean shutdown on SIGTERM/Ctrl+C and a Temporal connect with retry."""

import asyncio
import contextlib
import signal

import structlog
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError

from finn.config import Settings

log = structlog.get_logger(__name__)

CONNECT_MAX_BACKOFF_SECONDS = 30.0


def install_stop_handlers() -> asyncio.Event:
    """Return an event that is set on SIGTERM (docker stop) or SIGINT (Ctrl+C)."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop: fall back to a plain handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    return stop


async def connect(settings: Settings, stop: asyncio.Event | None = None) -> Client | None:
    """Connect to Temporal, retrying with exponential backoff. Returns None if stopped first."""
    stop = stop or asyncio.Event()
    # Standard Temporal env/config-file settings first (namespace, TLS, ...); TEMPORAL_ADDRESS
    # from our settings always wins so `.env` and the compose environment behave the same.
    config = ClientConfig.load_client_connect_config()
    config["target_host"] = settings.temporal_address
    delay = 1.0
    while not stop.is_set():
        try:
            # Pydantic models as payloads (Decimals as strings)
            client = await Client.connect(**config, data_converter=pydantic_data_converter)
        except (RuntimeError, RPCError) as exc:  # connect failures surface as RuntimeError
            log.warning(
                "temporal not reachable, retrying",
                address=settings.temporal_address,
                retry_in_s=delay,
                error=str(exc),
            )
            with contextlib.suppress(TimeoutError):  # sleep `delay`, but wake early on stop
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, CONNECT_MAX_BACKOFF_SECONDS)
        else:
            log.info("connected to temporal", address=settings.temporal_address)
            return client
    return None
