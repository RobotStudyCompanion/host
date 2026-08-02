"""Entry point for ``python -m rsc_host`` / ``rsc-host``.

Boots peripherals (fake or pi HAL), starts the WebSocket server, waits for
SIGINT/SIGTERM, then shuts everything down cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from pydantic import BaseModel

from rsc_host import __version__
from rsc_host.auth import TokenAuth
from rsc_host.config import Config, load_from_env
from rsc_host.dispatch import default_dispatcher, verb
from rsc_host.events import default_bus
from rsc_host.peripherals.registry import setup as setup_peripherals
from rsc_host.server import Server, build_ssl_context

log = logging.getLogger(__name__)


# ---- Baseline verbs ----
#
# ``status`` and ``ping`` are always available regardless of peripheral state.
# Peripheral-specific verbs are registered by peripherals.registry.setup().


class _EmptyArgs(BaseModel):
    pass


@verb("status", args_model=_EmptyArgs)
async def _status(_args: _EmptyArgs) -> dict:
    """Return daemon version, registered verbs, and subscriber count."""
    return {
        "version": __version__,
        "verbs": default_dispatcher.registered_verbs(),
        "subscribers": default_bus.subscriber_count(),
    }


@verb("ping", args_model=_EmptyArgs)
async def _ping(_args: _EmptyArgs) -> dict:
    """Trivial liveness verb."""
    return {"pong": True}


# ---- Bootstrap ----


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _run(config: Config) -> None:
    auth = TokenAuth(config.token)
    ssl_ctx = None
    if config.tls_enabled:
        assert config.tls_cert and config.tls_key
        ssl_ctx = build_ssl_context(config.tls_cert, config.tls_key)

    log.info(
        "rsc-host %s starting (backend=%s, tls=%s)",
        __version__,
        config.backend,
        config.tls_enabled,
    )

    # Boot peripherals BEFORE the server, so verbs are registered and the
    # arcade button's edge callback is live before any client can connect.
    peripherals = await setup_peripherals(
        default_dispatcher, default_bus, backend=config.backend
    )

    server = Server(
        dispatcher=default_dispatcher,
        bus=default_bus,
        auth=auth,
        bind=config.bind,
        port=config.port,
        ssl_context=ssl_ctx,
    )

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass  # Windows: no signal handlers on the loop

    await server.start()
    try:
        await shutdown.wait()
    finally:
        log.info("shutdown requested; stopping")
        await server.stop()
        await peripherals.stop()


def main() -> int:
    """Entry point exposed via [project.scripts]. Returns exit code."""
    try:
        config = load_from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(config.log_level)

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
