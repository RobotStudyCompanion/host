"""Client library for the rsc-host daemon.

Usage::

    import asyncio
    from rsc_host.client import connect

    async def main():
        async with connect("shiny", token="dev") as robot:
            await robot.ping()
            await robot.flipper_left(0.5, ramp_ms=500)
            await asyncio.sleep(2)
            await robot.flipper_left_stop()

    asyncio.run(main())

See :mod:`rsc_host.client.connection` for the full :class:`Robot` API and
:mod:`rsc_host.client.discovery` for mDNS enumeration.
"""
from __future__ import annotations

from rsc_host.client.connection import Robot, connect
from rsc_host.client.discovery import RobotInfo, discover, resolve
from rsc_host.client.errors import ConnectionRefused, DisconnectedError, RSCError

__all__ = [
    "connect",
    "Robot",
    "RobotInfo",
    "discover",
    "resolve",
    "RSCError",
    "DisconnectedError",
    "ConnectionRefused",
]
