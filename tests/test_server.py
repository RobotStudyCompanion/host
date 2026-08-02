"""End-to-end server integration tests.

Boots a real :class:`Server` on a random port, connects via the `websockets`
client, and asserts on real wire traffic. No mocks — closest thing we have
to a smoke test of the whole daemon short of running :mod:`rsc_host.__main__`.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import websockets
from pydantic import BaseModel
from websockets.exceptions import InvalidStatus

from rsc_host.auth import TokenAuth
from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.protocol import Ack, Event
from rsc_host.server import Server


TOKEN = "test-token-12345"


class _EchoArgs(BaseModel):
    message: str


@pytest.fixture
async def running_server() -> AsyncIterator[tuple[Server, Dispatcher, EventBus, int]]:
    """Fresh server + dispatcher + event bus on an ephemeral port."""
    dispatcher = Dispatcher()
    bus = EventBus()
    auth = TokenAuth(TOKEN)

    @dispatcher.verb("echo", args_model=_EchoArgs)
    async def _echo(args: _EchoArgs) -> dict:
        return {"echoed": args.message}

    server = Server(dispatcher, bus, auth, bind="127.0.0.1", port=0)
    await server.start()
    # Discover the ephemeral port the OS actually gave us.
    port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        yield server, dispatcher, bus, port
    finally:
        await server.stop()


async def _connect(port: int, token: str = TOKEN):
    """Connect with a bearer token via subprotocol negotiation."""
    return await websockets.connect(
        f"ws://127.0.0.1:{port}",
        subprotocols=["bearer", token],  # type: ignore[list-item]
    )


class TestAuthentication:
    async def test_valid_token_accepted(self, running_server) -> None:
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            # If we got here, handshake succeeded.
            assert ws.subprotocol == "bearer"
        finally:
            await ws.close()

    async def test_wrong_token_rejected(self, running_server) -> None:
        _, _, _, port = running_server
        with pytest.raises(InvalidStatus) as excinfo:
            await _connect(port, token="not-the-token")
        assert excinfo.value.response.status_code == 401

    async def test_missing_subprotocol_rejected(self, running_server) -> None:
        _, _, _, port = running_server
        with pytest.raises(InvalidStatus) as excinfo:
            await websockets.connect(f"ws://127.0.0.1:{port}")
        assert excinfo.value.response.status_code == 401


class TestDispatch:
    async def test_command_ack_roundtrip(self, running_server) -> None:
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "cmd",
                        "id": "c1",
                        "verb": "echo",
                        "args": {"message": "hello"},
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            ack = Ack.model_validate_json(raw)
            assert ack.ok is True
            assert ack.id == "c1"
            assert ack.result == {"echoed": "hello"}
        finally:
            await ws.close()

    async def test_unknown_verb_returns_error_ack(self, running_server) -> None:
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            await ws.send(
                json.dumps(
                    {"type": "cmd", "id": "c2", "verb": "nonexistent", "args": {}}
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            ack = Ack.model_validate_json(raw)
            assert ack.ok is False
            assert ack.code == "UNKNOWN_VERB"
        finally:
            await ws.close()

    async def test_malformed_json_returns_error_ack(self, running_server) -> None:
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            await ws.send("this is not json {{{")
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            ack = Ack.model_validate_json(raw)
            assert ack.ok is False
            assert ack.code == "INVALID_MESSAGE"
        finally:
            await ws.close()

    async def test_missing_id_returns_error_ack(self, running_server) -> None:
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            # Valid JSON but wrong shape — missing required 'id' field.
            await ws.send(json.dumps({"type": "cmd", "verb": "echo", "args": {}}))
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            ack = Ack.model_validate_json(raw)
            assert ack.ok is False
            assert ack.code == "INVALID_MESSAGE"
        finally:
            await ws.close()

    async def test_rx_loop_survives_bad_input(self, running_server) -> None:
        # Server must keep the connection open after a validation error and
        # continue processing subsequent commands.
        _, _, _, port = running_server
        ws = await _connect(port)
        try:
            await ws.send("garbage")
            await asyncio.wait_for(ws.recv(), timeout=2.0)  # discard error ack

            await ws.send(
                json.dumps(
                    {
                        "type": "cmd",
                        "id": "c3",
                        "verb": "echo",
                        "args": {"message": "still here"},
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            ack = Ack.model_validate_json(raw)
            assert ack.ok is True
            assert ack.result == {"echoed": "still here"}
        finally:
            await ws.close()


class TestEvents:
    async def test_event_reaches_connected_client(self, running_server) -> None:
        _, _, bus, port = running_server
        ws = await _connect(port)
        try:
            # Give the server a moment to complete its subscribe() setup.
            await asyncio.sleep(0.05)
            await bus.publish(Event(topic="button.press", source="host", data={}))
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            evt = Event.model_validate_json(raw)
            assert evt.topic == "button.press"
            assert evt.source == "host"
        finally:
            await ws.close()

    async def test_event_fans_out_to_multiple_clients(self, running_server) -> None:
        _, _, bus, port = running_server
        ws1 = await _connect(port)
        ws2 = await _connect(port)
        try:
            await asyncio.sleep(0.05)
            await bus.publish(Event(topic="host_vol", source="cyd", data={"v": 42}))
            r1 = await asyncio.wait_for(ws1.recv(), timeout=2.0)
            r2 = await asyncio.wait_for(ws2.recv(), timeout=2.0)
            e1 = Event.model_validate_json(r1)
            e2 = Event.model_validate_json(r2)
            assert e1.topic == "host_vol" == e2.topic
            assert e1.data == {"v": 42} == e2.data
        finally:
            await ws1.close()
            await ws2.close()

    async def test_events_and_acks_interleave(self, running_server) -> None:
        # Publish an event, send a command; client should receive both — the
        # order isn't strictly deterministic under the concurrent send/receive
        # loops, but both must arrive.
        _, _, bus, port = running_server
        ws = await _connect(port)
        try:
            await asyncio.sleep(0.05)
            await bus.publish(Event(topic="tick", source="host", data={}))
            await ws.send(
                json.dumps(
                    {
                        "type": "cmd",
                        "id": "c4",
                        "verb": "echo",
                        "args": {"message": "x"},
                    }
                )
            )
            got_event = False
            got_ack = False
            for _ in range(2):
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                payload = json.loads(raw)
                if payload["type"] == "event":
                    got_event = True
                elif payload["type"] == "ack":
                    got_ack = True
            assert got_event and got_ack
        finally:
            await ws.close()
