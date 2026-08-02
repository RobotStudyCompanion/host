"""End-to-end integration for the client library.

Boots a real :class:`~rsc_host.server.Server` on an ephemeral port with fake
peripherals, then exercises the client library against it. Closest thing to
running the daemon and pointing a real script at it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from rsc_host.auth import TokenAuth
from rsc_host.client import RSCError, connect
from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.protocol import Event
from rsc_host.server import Server

TOKEN = "test-client-token"


class _EchoArgs(BaseModel):
    message: str


class _EmptyArgs(BaseModel):
    pass


class _HistoryArgs(BaseModel):
    since_seq: int | None = None
    limit: int | None = None
    topic: str | None = None


@pytest.fixture
async def running_daemon() -> AsyncIterator[tuple[Dispatcher, EventBus, int]]:
    """Real server on an ephemeral port with a minimal verb set."""
    dispatcher = Dispatcher()
    bus = EventBus()
    auth = TokenAuth(TOKEN)

    @dispatcher.verb("ping", args_model=_EmptyArgs)
    async def _ping(_args: _EmptyArgs) -> dict:
        return {"pong": True}

    @dispatcher.verb("echo", args_model=_EchoArgs)
    async def _echo(args: _EchoArgs) -> dict:
        return {"echoed": args.message}

    @dispatcher.verb("boom", args_model=_EmptyArgs)
    async def _boom(_args: _EmptyArgs) -> dict:
        raise ValueError("oops")

    @dispatcher.verb("events.history", args_model=_HistoryArgs)
    async def _history(args: _HistoryArgs) -> dict:
        events = bus.history(since_seq=args.since_seq, limit=args.limit)
        if args.topic is not None:
            events = [e for e in events if e.topic == args.topic]
        return {
            "events": [e.model_dump(mode="json") for e in events],
            "latest_seq": bus.latest_seq(),
        }

    server = Server(dispatcher, bus, auth, bind="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        yield dispatcher, bus, port
    finally:
        await server.stop()


class TestConnect:
    async def test_ping_roundtrip(self, running_daemon) -> None:
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            result = await robot.ping()
            assert result == {"pong": True}

    async def test_generic_call(self, running_daemon) -> None:
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            result = await robot.call("echo", message="hi")
            assert result == {"echoed": "hi"}

    async def test_bad_token_refused(self, running_daemon) -> None:
        from rsc_host.client import ConnectionRefused
        _, _, port = running_daemon
        with pytest.raises(ConnectionRefused, match="unauthorized"):
            async with connect(f"ws://127.0.0.1:{port}", token="wrong"):
                pass

    async def test_missing_token_refused(self, running_daemon, monkeypatch) -> None:
        from rsc_host.client import ConnectionRefused
        monkeypatch.delenv("RSC_TOKEN", raising=False)
        _, _, port = running_daemon
        with pytest.raises(ConnectionRefused, match="no token"):
            async with connect(f"ws://127.0.0.1:{port}"):
                pass

    async def test_token_from_env(self, running_daemon, monkeypatch) -> None:
        monkeypatch.setenv("RSC_TOKEN", TOKEN)
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}") as robot:
            await robot.ping()


class TestErrors:
    async def test_unknown_verb_raises_rsc_error(self, running_daemon) -> None:
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            with pytest.raises(RSCError) as excinfo:
                await robot.call("nonexistent")
            assert excinfo.value.code == "UNKNOWN_VERB"
            assert excinfo.value.verb == "nonexistent"

    async def test_handler_exception_becomes_internal_error(
        self, running_daemon
    ) -> None:
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            with pytest.raises(RSCError) as excinfo:
                await robot.call("boom")
            assert excinfo.value.code == "INTERNAL_ERROR"

    async def test_bad_args_raises_rsc_error(self, running_daemon) -> None:
        _, _, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            # 'echo' requires 'message'; sending nothing should INVALID_ARGS.
            with pytest.raises(RSCError) as excinfo:
                await robot.call("echo")
            assert excinfo.value.code == "INVALID_ARGS"


class TestEvents:
    async def test_events_receive_live(self, running_daemon) -> None:
        _, bus, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            # Give the rx loop and subscribe a moment to attach.
            await asyncio.sleep(0.05)

            async def collect() -> Event:
                async for e in robot.events():
                    return e
                raise AssertionError("no event received")

            collect_task = asyncio.create_task(collect())
            await asyncio.sleep(0.05)
            await bus.publish(Event(topic="button.press", source="host"))
            evt = await asyncio.wait_for(collect_task, timeout=2.0)
            assert evt.topic == "button.press"
            assert evt.seq == 1

    async def test_events_topic_filter(self, running_daemon) -> None:
        _, bus, port = running_daemon
        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            await asyncio.sleep(0.05)

            async def collect() -> Event:
                async for e in robot.events(topic="button.press"):
                    return e
                raise AssertionError("no event received")

            collect_task = asyncio.create_task(collect())
            await asyncio.sleep(0.05)
            # Publish a non-matching event; then a matching one.
            await bus.publish(Event(topic="other", source="host"))
            await bus.publish(Event(topic="button.press", source="host"))
            evt = await asyncio.wait_for(collect_task, timeout=2.0)
            assert evt.topic == "button.press"

    async def test_events_replay_since_seq(self, running_daemon) -> None:
        _, bus, port = running_daemon
        # Publish events *before* the client connects.
        await bus.publish(Event(topic="early-1", source="host"))
        await bus.publish(Event(topic="early-2", source="host"))
        await bus.publish(Event(topic="early-3", source="host"))

        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            replayed: list[Event] = []

            async def collect() -> None:
                async for e in robot.events(replay_since_seq=2):
                    replayed.append(e)
                    if len(replayed) >= 2:
                        return

            await asyncio.wait_for(collect(), timeout=2.0)
            # Replay includes seqs 2 and 3.
            assert [e.seq for e in replayed] == [2, 3]

    async def test_events_replay_history_shortcut(self, running_daemon) -> None:
        _, bus, port = running_daemon
        await bus.publish(Event(topic="a", source="host"))
        await bus.publish(Event(topic="b", source="host"))

        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            got: list[Event] = []

            async def collect() -> None:
                async for e in robot.events(replay_history=True):
                    got.append(e)
                    if len(got) >= 2:
                        return

            await asyncio.wait_for(collect(), timeout=2.0)
            assert [e.topic for e in got] == ["a", "b"]


class TestTimeoutAndDisconnect:
    async def test_call_timeout_raises(self, running_daemon) -> None:
        # Register a verb that never completes, so we can trigger a real timeout.
        dispatcher, _, port = running_daemon

        @dispatcher.verb("hang", args_model=_EmptyArgs)
        async def _hang(_args: _EmptyArgs) -> dict:
            await asyncio.sleep(60)  # blocks past any reasonable test timeout
            return {}

        async with connect(f"ws://127.0.0.1:{port}", token=TOKEN) as robot:
            with pytest.raises(TimeoutError):
                await robot.call("hang", timeout=0.1)
