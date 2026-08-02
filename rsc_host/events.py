"""Async event bus for fanning host-side events out to subscribed clients.

Producers (peripherals, the CYD bridge, the host itself) call :meth:`EventBus.publish`
with an :class:`~rsc_host.protocol.Event`. The bus multicasts to every currently
subscribed client via its own :class:`asyncio.Queue`.

Design constraints:

* **Non-blocking publish.** Producers must never be back-pressured by slow
  clients. If a subscriber's queue is full, the event is dropped *for that
  subscriber only* and a warning is logged. Other subscribers are unaffected.
* **Subscribe via async context manager.** :meth:`subscribe` yields a queue
  and cleans up on exit — no `finally` bookkeeping at call sites, no
  subscription leaks if a client disconnects mid-await.
* **No topic filtering here.** Every subscriber sees every event; clients
  filter on their side if they care. Keeps the bus trivially small and the
  wire contract explicit. If filtering ever becomes a bottleneck (unlikely
  at LAN-client scale), we add it then.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from rsc_host.protocol import Event

log = logging.getLogger(__name__)

# Per-subscriber queue depth. 128 events buffered before we start dropping.
# For reference: telemetry at 10 Hz for 12 s of client stall = 120 events.
_DEFAULT_QUEUE_MAXSIZE = 128


class EventBus:
    """Fan-out event bus. One instance per host, shared across all producers."""

    def __init__(self, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        """Multicast ``event`` to every current subscriber. Never blocks.

        A subscriber with a full queue silently loses this event (logged at
        WARNING). This is deliberate: a stuck client cannot stall the host.
        """
        # Snapshot the subscriber set under the lock so concurrent
        # (un)subscription during publish doesn't mutate the iteration target.
        async with self._lock:
            targets = tuple(self._subscribers)

        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "event bus: dropping event for slow subscriber "
                    "(topic=%s, source=%s)",
                    event.topic,
                    event.source,
                )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        """Register a new subscriber queue for the lifetime of the ``async with``.

        Usage::

            async with bus.subscribe() as queue:
                while True:
                    event = await queue.get()
                    # forward to WebSocket client
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        """Number of currently-registered subscribers. Test / status use."""
        return len(self._subscribers)


#: Module-level default. Peripherals and the CYD bridge publish here; the server
#: subscribes here per client. Distinct from the dispatcher singleton but same
#: pattern.
default_bus = EventBus()
