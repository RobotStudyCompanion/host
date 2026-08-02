"""LAN discovery for the client — find RSCs by mDNS instead of hard-coding hosts.

Uses ``zeroconf.asyncio.AsyncZeroconf`` for browsing.

Two entry points:

* :func:`resolve` — look up one specific robot by name (case-insensitive).
  Returns a :class:`RobotInfo` if found within the timeout, or ``None``.

* :func:`discover` — async-iterate every RSC service on the LAN as they
  appear. Yields :class:`RobotInfo` records; caller decides when to stop.

Both are best-effort. If zeroconf isn't importable or no network is available,
the caller can still connect via explicit hostname/URL.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

SERVICE_TYPE = "_rsc-host._tcp.local."


@dataclass(frozen=True, slots=True)
class RobotInfo:
    """One discovered RSC host.

    Attributes:
        name:    Robot name (from the mDNS instance / TXT record).
        host:    Hostname (``rsc-shiny.local``) — preferred for URL building.
        address: IPv4 address the service is bound to (fallback).
        port:    TCP port.
        tls:     Whether the daemon advertises TLS.
    """

    name: str
    host: str
    address: str
    port: int
    tls: bool

    @property
    def proto(self) -> str:
        return "wss" if self.tls else "ws"

    @property
    def url(self) -> str:
        """WebSocket URL for the control channel."""
        return f"{self.proto}://{self.host}:{self.port}"


def _info_from_zeroconf(info) -> RobotInfo | None:
    """Convert a zeroconf ``ServiceInfo`` to :class:`RobotInfo` — or None if malformed."""
    if info is None:
        return None
    addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
    if not addresses:
        return None
    props = info.properties or {}

    def _get(key: bytes, default: str = "") -> str:
        raw = props.get(key, default.encode() if isinstance(default, str) else default)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    robot_name = _get(b"robot_name") or info.name.split(".", 1)[0]
    tls = _get(b"tls").lower() == "true"
    host = info.server.rstrip(".") if info.server else f"{robot_name}.local"

    return RobotInfo(
        name=robot_name,
        host=host,
        address=addresses[0],
        port=info.port or 8765,
        tls=tls,
    )


async def resolve(name: str, timeout: float = 3.0) -> RobotInfo | None:
    """Look up one robot by name. Case-insensitive.

    Returns the first matching :class:`RobotInfo` within ``timeout``, or None.
    """
    target = name.lower()
    found: RobotInfo | None = None
    event = asyncio.Event()

    async for info in discover(timeout=timeout):
        if info.name.lower() == target:
            found = info
            event.set()
            break
    return found


async def discover(timeout: float = 3.0) -> AsyncIterator[RobotInfo]:
    """Yield each ``_rsc-host._tcp`` service on the LAN as it's discovered.

    Runs for ``timeout`` seconds, then stops. Duplicate names (same robot
    reachable via multiple interfaces) may appear more than once — dedupe
    on ``name`` if that matters.
    """
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    except ImportError:
        log.warning("zeroconf not installed; discovery unavailable")
        return

    zc = AsyncZeroconf()
    queue: asyncio.Queue[RobotInfo] = asyncio.Queue()

    async def _on_change(zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        try:
            info = await zc.async_get_service_info(service_type, name)
            robot = _info_from_zeroconf(info)
            if robot is not None:
                await queue.put(robot)
        except Exception:
            log.exception("failed to resolve %s", name)

    def _handler(zeroconf, service_type, name, state_change):
        asyncio.create_task(_on_change(zeroconf, service_type, name, state_change))

    browser = AsyncServiceBrowser(
        zc.zeroconf, [SERVICE_TYPE], handlers=[_handler]
    )
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                info = await asyncio.wait_for(queue.get(), timeout=remaining)
                yield info
            except asyncio.TimeoutError:
                break
    finally:
        await browser.async_cancel()
        await zc.async_close()
