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
        await bus.publish(_mk("orphan"))

    async def test_publish_never_blocks_on_full_queue(self) -> None:
        bus = EventBus(queue_maxsize=2)
        async with bus.subscribe() as _queue:
            for i in range(10):
                await asyncio.wait_for(
                    bus.publish(_mk(f"topic-{i}")),
                    timeout=1.0,
                )

    async def test_slow_subscriber_does_not_affect_fast_one(self) -> None:
        bus = EventBus(queue_maxsize=2)
        async with bus.subscribe() as slow, bus.subscribe() as fast:
            drain_task = asyncio.create_task(_drain(fast, count=5))
            for i in range(5):
                await bus.publish(_mk(f"t-{i}"))
                await asyncio.sleep(0)   # yield so drain_task can pull from fast
            received = await asyncio.wait_for(drain_task, timeout=1.0)
            assert len(received) == 5
            assert slow.qsize() == 2


class TestSequenceNumbers:
    async def test_seq_starts_at_one(self) -> None:
        bus = EventBus()
        assert bus.latest_seq() == 1

    async def test_seq_monotonic(self) -> None:
        bus = EventBus()
        seqs = []
        async with bus.subscribe() as q:
            for i in range(5):
                await bus.publish(_mk(f"t-{i}"))
                seqs.append((await q.get()).seq)
        assert seqs == [1, 2, 3, 4, 5]

    async def test_latest_seq_reflects_publish_count(self) -> None:
        bus = EventBus()
        for i in range(3):
            await bus.publish(_mk(f"t-{i}"))
        assert bus.latest_seq() == 4  # next seq to be assigned

    async def test_seq_survives_subscriber_churn(self) -> None:
        bus = EventBus()
        await bus.publish(_mk("first"))
        async with bus.subscribe() as q:
            await bus.publish(_mk("second"))
            e = await q.get()
        # Second event should have seq 2, not 1 — sub churn doesn't reset counter.
        assert e.seq == 2

    async def test_publish_preserves_incoming_seq_none(self) -> None:
        # Bus assigns seq; caller shouldn't have to.
        bus = EventBus()
        async with bus.subscribe() as q:
            await bus.publish(Event(topic="x", source="host"))  # no seq
            e = await q.get()
        assert e.seq == 1


class TestHistory:
    async def test_history_empty_by_default(self) -> None:
        bus = EventBus()
        assert bus.history() == []

    async def test_history_records_publishes(self) -> None:
        bus = EventBus()
        for i in range(3):
            await bus.publish(_mk(f"t-{i}"))
        h = bus.history()
        assert [e.topic for e in h] == ["t-0", "t-1", "t-2"]
        assert [e.seq for e in h] == [1, 2, 3]

    async def test_history_bounded_by_ring_size(self) -> None:
        bus = EventBus(history_size=3)
        for i in range(10):
            await bus.publish(_mk(f"t-{i}"))
        h = bus.history()
        assert len(h) == 3
        # Only the last 3 remain — oldest first.
        assert [e.topic for e in h] == ["t-7", "t-8", "t-9"]
        assert [e.seq for e in h] == [8, 9, 10]

    async def test_history_since_seq(self) -> None:
        bus = EventBus()
        for i in range(5):
            await bus.publish(_mk(f"t-{i}"))
        h = bus.history(since_seq=3)
        assert [e.seq for e in h] == [3, 4, 5]

    async def test_history_limit(self) -> None:
        bus = EventBus()
        for i in range(10):
            await bus.publish(_mk(f"t-{i}"))
        h = bus.history(limit=3)
        # Limit takes from the *tail* — most recent 3.
        assert [e.seq for e in h] == [8, 9, 10]

    async def test_history_since_seq_and_limit(self) -> None:
        bus = EventBus()
        for i in range(10):
            await bus.publish(_mk(f"t-{i}"))
        h = bus.history(since_seq=5, limit=2)
        # since_seq filters to seqs 5..10 (6 items), limit keeps tail 2.
        assert [e.seq for e in h] == [9, 10]

    async def test_history_returns_snapshot_not_reference(self) -> None:
        # Mutating the returned list must not affect subsequent history() calls.
        bus = EventBus()
        await bus.publish(_mk("t"))
        h1 = bus.history()
        h1.clear()
        h2 = bus.history()
        assert len(h2) == 1


async def _drain(queue: asyncio.Queue[Event], count: int) -> list[Event]:
    out: list[Event] = []
    for _ in range(count):
        out.append(await queue.get())
    return out
