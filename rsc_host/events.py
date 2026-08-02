"""Async event bus with fan-out and a bounded history ring buffer.

Producers (peripherals, the CYD bridge, the host itself) call
:meth:`EventBus.publish` with an :class:`~rsc_host.protocol.Event`. The bus
assigns the event a monotonic sequence number, appends it to a bounded ring
buffer of recent events, and multicasts to every currently subscribed client
via its own :class:`asyncio.Queue`.

Design constraints:

* **Non-blocking publish.** Producers must never be back-pressured by slow
  clients. If a subscriber's queue is full, the event is dropped *for that
  subscriber only* and a warning is logged. Other subscribers are unaffected.
* **Subscribe via async context manager.** :meth:`subscribe` yields a queue
  and cleans up on exit — no ``finally`` bookkeeping at call sites, no
  subscription leaks if a client disconnects mid-await.
* **No topic filtering here.** Every subscriber sees every event; clients
  filter on their side if they care.
* **Bounded history.** The bus keeps the last ``history_size`` events in a
  ring buffer. Clients can request replay via :meth:`history` — filtered by
  minimum sequence number or by count. Enables reconnect recovery and
  "what did I miss" flows without unbounded memory growth.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from rsc_host.protocol import Event

log = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 128
_DEFAULT_HISTORY_SIZE = 256


class EventBus:
    """Fan-out event bus. One instance per host, shared across all producers.

    Args:
        queue_maxsize:  Per-subscriber queue depth before dropping.
        history_size:   Ring-buffer depth for :meth:`history` replay.
    """

    def __init__(
        self,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        history_size: int = _DEFAULT_HISTORY_SIZE,
    ) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_maxsize = queue_maxsize
        self._history: deque[Event] = deque(maxlen=history_size)
        self._next_seq = 1
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        """Multicast ``event`` to every current subscriber. Never blocks.

        The event is stamped with a monotonic sequence number and appended to
        the history ring buffer before fanout.
        """
        async with self._lock:
            stamped = event.model_copy(update={"seq": self._next_seq})
            self._next_seq += 1
            self._history.append(stamped)
            targets = tuple(self._subscribers)

        for queue in targets:
            try:
                queue.put_nowait(stamped)
            except asyncio.QueueFull:
                log.warning(
                    "event bus: dropping event for slow subscriber "
                    "(topic=%s, source=%s, seq=%d)",
                    stamped.topic,
                    stamped.source,
                    stamped.seq or -1,
                )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        """Register a new subscriber queue for the lifetime of the ``async with``."""
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

    def latest_seq(self) -> int:
        """Sequence number that will be assigned to the *next* published event.

        The most recently published event has ``latest_seq() - 1``. Zero
        events published so far ⇒ ``latest_seq() == 1``.
        """
        return self._next_seq

    def history(
        self,
        *,
        since_seq: int | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Snapshot of recent events, oldest first.

        Args:
            since_seq: Return only events with ``seq >= since_seq``. If None,
                       return all buffered events.
            limit:     If set, return at most this many events (from the tail
                       of the filtered set — most recent).
        """
        buffered = list(self._history)
        if since_seq is not None:
            buffered = [e for e in buffered if e.seq is not None and e.seq >= since_seq]
        if limit is not None and len(buffered) > limit:
            buffered = buffered[-limit:]
        return buffered


#: Module-level default. Peripherals and the CYD bridge publish here; the server
#: subscribes here per client.
default_bus = EventBus()
