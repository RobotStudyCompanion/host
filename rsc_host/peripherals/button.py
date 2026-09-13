"""Arcade button: subscribes to GPIO edges and republishes as ``button.press``
and ``button.release`` events.

Debouncing: even with the HAL backend advertising debounced edges (gpiozero on
the Pi does this natively), we apply a small guard window here so tests and
alternative backends behave consistently. Edges within the guard window of
the previous accepted edge for the same direction are silently dropped.

Events emitted:

* ``button.press``   — ``{"pin": <int>}``  on RISING edge
* ``button.release`` — ``{"pin": <int>}``  on FALLING edge

The peripheral is level-agnostic — it publishes rising/falling regardless of
whether the button is wired active-high or active-low. Consumers care about
press/release, which map to rising/falling for the arcade button as wired on
the HAT (active-high).
"""
from __future__ import annotations

import asyncio
import logging
import time

from rsc_host.events import EventBus
from rsc_host.hal.base import GpioInputBackend
from rsc_host.hal.types import Edge, GpioEdge
from rsc_host.protocol import Event

log = logging.getLogger(__name__)

# Software debounce guard. gpiozero's default is 0ms; 20ms is a comfortable
# floor that eats mechanical bounce without noticeable input lag.
_DEBOUNCE_NS = 20_000_000  # 20 ms


class ArcadeButton:
    """Arcade button wired to ``pin`` on the GPIO input backend."""

    def __init__(
        self,
        pin: int,
        backend: GpioInputBackend,
        bus: EventBus,
    ) -> None:
        self._pin = pin
        self._backend = backend
        self._bus = bus
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_edge_ns: dict[Edge, int] = {}
        # asyncio keeps only a weak reference to a running task. Without a
        # strong one here, a publish scheduled from the edge callback can be
        # garbage collected before it runs — a press that vanishes under load
        # and never under test.
        self._pending: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Register the edge callback. Backend must already be started."""
        self._loop = asyncio.get_running_loop()
        await self._backend.on_edge(self._pin, self._on_edge)

    def _on_edge(self, event: GpioEdge) -> None:
        """Runs on the loop thread (per HAL contract). Publish via create_task
        so the callback returns quickly — the HAL is free to fire the next edge."""
        # Debounce: drop edges of the same direction within the guard window.
        last = self._last_edge_ns.get(event.edge, 0)
        if event.timestamp_ns - last < _DEBOUNCE_NS:
            return
        self._last_edge_ns[event.edge] = event.timestamp_ns

        topic = "button.press" if event.edge == Edge.RISING else "button.release"
        loop = self._loop
        if loop is None:
            log.warning("button edge received before start(); dropping")
            return
        task = loop.create_task(
            self._bus.publish(
                Event(
                    topic=topic,
                    source="host",
                    data={"pin": event.pin},
                )
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
