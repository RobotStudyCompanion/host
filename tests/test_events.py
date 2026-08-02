"""EventBus behaviour: fan-out, subscription lifecycle, non-blocking publish."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.protocol import Event


def _mk(topic: str) -> Event:
    return Event(topic=topic, source="host", data={})


class TestSubscriptionLifecycle:
    async def test_subscribe_adds_and_removes(self) -> None:
        bus = EventBus()
        assert bus.subscriber_count() == 0
        async with bus.subscribe() as queue:
            assert bus.subscriber_count() == 1
            assert isinstance(queue, asyncio.Queue)
        assert bus.subscriber_count() == 0

    async def test_subscribe_removes_on_exception(self) -> None:
        # If the client-side loop raises, we still want the subscription cleaned up.
        bus = EventBus()
        with pytest.raises(RuntimeError, match="client blew up"):
            async with bus.subscribe():
                assert bus.subscriber_count() == 1
                raise RuntimeError("client blew up")
        assert bus.subscriber_count() == 0

    async def test_multiple_subscribers_independent(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as q1, bus.subscribe() as q2:
            assert bus.subscriber_count() == 2
            assert q1 is not q2
        assert bus.subscriber_count() == 0


class TestPublish:
    async def test_publish_to_single_subscriber(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as queue:
            await bus.publish(_mk("host_vol"))
            event = await queue.get()
            assert event.topic == "host_vol"

    async def test_publish_fans_out_to_all_subscribers(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as q1, bus.subscribe() as q2:
            await bus.publish(_mk("button.press"))
            e1 = await q1.get()
            e2 = await q2.get()
            assert e1.topic == "button.press"
            assert e2.topic == "button.press"

    async def test_publish_with_no_subscribers_noop(self) -> None:
        bus = EventBus()
        # Must not raise, must not block.
        await bus.publish(_mk("orphan"))

    async def test_publish_never_blocks_on_full_queue(self) -> None:
        # Non-blocking guarantee: a slow subscriber must not stall the producer.
        bus = EventBus(queue_maxsize=2)
        async with bus.subscribe() as _queue:
            # Fill the queue past capacity. Publish must return promptly on
            # every call; overflow events are dropped for this subscriber.
            for i in range(10):
                await asyncio.wait_for(
                    bus.publish(_mk(f"topic-{i}")),
                    timeout=1.0,
                )

    async def test_slow_subscriber_does_not_affect_fast_one(self) -> None:
        bus = EventBus(queue_maxsize=2)
        async with bus.subscribe() as slow, bus.subscribe() as fast:
            # Publish 5 events. `slow` will drop after 2; `fast` drains as it goes.
            drain_task = asyncio.create_task(_drain(fast, count=5))
            for i in range(5):
                await bus.publish(_mk(f"t-{i}"))
                await asyncio.sleep(0)   # yield so drain_task can pull from `fast`
            received = await asyncio.wait_for(drain_task, timeout=1.0)
            assert len(received) == 5
            # Slow subscriber holds the first two, rest dropped:
            assert slow.qsize() == 2


async def _drain(queue: asyncio.Queue[Event], count: int) -> list[Event]:
    out: list[Event] = []
    for _ in range(count):
        out.append(await queue.get())
    return out
