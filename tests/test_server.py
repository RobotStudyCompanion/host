"""End-to-end server integration tests.

Boots a real :class:`Server` on a random port, connects via the `websockets`
client, and asserts on real wire traffic. No mocks — closest thing we have
to a smoke test of the whole daemon short of running :mod:`rsc_host.__main__`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import pytest
import websockets
from pydantic import BaseModel
from websockets.exceptions import InvalidStatus

from rsc_host.auth import TokenAuth
from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.motion import MotionOwners
from rsc_host.peripherals.registry import setup
from rsc_host.protocol import Ack, Cmd, Event
from rsc_host.server import (
    CLOSE_TIMEOUT_S,
    MAX_QUEUED_FRAMES,
    PING_INTERVAL_S,
    PING_TIMEOUT_S,
    Server,
)
from rsc_host.state import StateStore


TOKEN = "test-token-12345"


class _EchoArgs(BaseModel):
    message: str


class _NoArgs(BaseModel):
    pass


@pytest.fixture
async def running_server() -> AsyncIterator[tuple[Server, Dispatcher, EventBus, int]]:
    """Fresh server + dispatcher + event bus on an ephemeral port."""
    dispatcher = Dispatcher()
    bus = EventBus()
    auth = TokenAuth(TOKEN)

    @dispatcher.verb("echo", args_model=_EchoArgs)
    async def _echo(args: _EchoArgs) -> dict:
        return {"echoed": args.message}

    server = Server(dispatcher, bus, auth, motion=MotionOwners(), bind="127.0.0.1", port=0)
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


# ---- Motion when a connection goes away ----


@pytest.fixture
async def robot(tmp_path) -> AsyncIterator:
    """A real server wired to the real verbs on the fake backend."""
    dispatcher = Dispatcher()
    bus = EventBus()
    peripherals = await setup(dispatcher, bus, backend="fake", state=StateStore(tmp_path))
    server = Server(
        dispatcher, bus, TokenAuth(TOKEN), bind="127.0.0.1", port=0,
        motion=peripherals.motion,
    )
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield _Robot(dispatcher, bus, peripherals, server, port)
    finally:
        await server.stop()
        await peripherals.stop()


class _Robot:
    def __init__(self, dispatcher, bus, peripherals, server, port) -> None:
        self.dispatcher = dispatcher
        self.bus = bus
        self.peripherals = peripherals
        self.server = server
        self.port = port
        self.servo = peripherals.servo_backend

    async def connect(self):
        ws = await _connect(self.port)
        # The subscribe happens after the handshake; let it land.
        await asyncio.sleep(0.02)
        return ws

    def speed(self, side: str = "left") -> float:
        return getattr(self.peripherals, f"flipper_{side}").current_speed


async def _call(ws, verb: str, **args) -> Ack:
    """Send a command and return its ack, skipping any events in between."""
    cmd_id = f"t-{verb}"
    await ws.send(json.dumps({"type": "cmd", "id": cmd_id, "verb": verb, "args": args}))
    while True:
        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        if payload["type"] == "ack" and payload["id"] == cmd_id:
            return Ack.model_validate(payload)


async def _until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def _settled() -> None:
    """Long enough for a connection that just closed to have finished cleaning up."""
    await asyncio.sleep(0.4)


class TestOwnerCloses:
    async def test_flipper_stopped_and_event_published(self, robot) -> None:
        owner = await robot.connect()
        watcher = await robot.connect()
        try:
            assert (await _call(owner, "flipper.left", speed=0.5)).ok
            assert await robot.servo.get_speed("left") == 0.5
            await owner.close()
            await _until(lambda: robot.speed() == 0.0)
            assert await robot.servo.get_speed("left") == 0.0
            while True:
                event = Event.model_validate_json(
                    await asyncio.wait_for(watcher.recv(), timeout=2.0)
                )
                if event.topic == "flipper.left.state" and event.data["speed"] == 0.0:
                    break
        finally:
            await watcher.close()

    async def test_hold_released(self, robot) -> None:
        owner = await robot.connect()
        assert (await _call(owner, "servo.hold", id="left", us=1500)).ok
        assert "left" in robot.servo.holding()
        await owner.close()
        await _until(lambda: not robot.servo.holding())

    async def test_only_what_it_owns_is_stopped(self, robot) -> None:
        a = await robot.connect()
        b = await robot.connect()
        try:
            await _call(a, "flipper.left", speed=0.5)
            await _call(b, "flipper.right", speed=0.7)
            await _call(b, "servo.hold", id="m3", us=1500)
            await a.close()
            await _until(lambda: robot.speed("left") == 0.0)
            await _settled()
            assert robot.speed("right") == 0.7
            assert "m3" in robot.servo.holding()
        finally:
            await b.close()

    async def test_running_ramp_is_cancelled(self, robot) -> None:
        owner = await robot.connect()
        # The ack is immediate; the ramp carries on in the background.
        assert (await _call(owner, "flipper.left", speed=1.0, ramp_ms=10_000)).ok
        await asyncio.sleep(0.05)
        await owner.close()
        await _until(lambda: robot.speed() == 0.0)
        await _settled()
        assert robot.speed() == 0.0
        assert await robot.servo.get_speed("left") == 0.0

    async def test_abrupt_loss_of_the_socket(self, robot) -> None:
        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await _call(owner, "servo.hold", id="right", us=1500)
        owner.transport.abort()
        await _until(lambda: robot.speed() == 0.0 and not robot.servo.holding())

    async def test_started_and_closed_without_waiting_for_the_ack(self, robot) -> None:
        owner = await robot.connect()
        await owner.send(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await owner.close()
        await _settled()
        assert robot.speed() == 0.0
        assert await robot.servo.get_speed("left") == 0.0

    async def test_table_is_empty_afterwards(self, robot) -> None:
        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await _call(owner, "servo.hold", id="right", us=1500)
        await owner.close()
        await _settled()
        assert robot.peripherals.motion.owned() == {}


class TestOwnershipRules:
    """One end-to-end check per verb. The rules themselves are in test_motion.py."""

    @pytest.mark.parametrize(
        ("verb", "args"),
        [
            ("flipper.left", {"speed": 0.0}),
            ("flipper.left.stop", {}),
            ("flipper.stop_all", {}),
        ],
    )
    async def test_stopped_elsewhere_then_owner_closes(self, robot, verb, args) -> None:
        a = await robot.connect()
        b = await robot.connect()
        try:
            await _call(a, "flipper.left", speed=0.5)
            assert (await _call(b, verb, **args)).ok
            assert robot.peripherals.motion.owned() == {}
            # Nothing left for A to stop, so closing must publish nothing at all.
            seq = robot.bus.latest_seq()
            await a.close()
            await _settled()
            assert robot.bus.history(since_seq=seq) == []
        finally:
            await b.close()

    @pytest.mark.parametrize("args", [{}, {"id": "left"}])
    async def test_hold_released_elsewhere_then_owner_closes(self, robot, args) -> None:
        a = await robot.connect()
        b = await robot.connect()
        try:
            await _call(a, "servo.hold", id="left", us=1500)
            released = await _call(b, "servo.hold.stop", **args)
            assert released.result == {"released": ["left"]}
            assert robot.peripherals.motion.owned() == {}
            await _call(b, "servo.hold", id="left", us=1500)
            await a.close()
            await _settled()
            assert "left" in robot.servo.holding()
        finally:
            await b.close()

    async def test_watcher_closing_changes_nothing(self, robot) -> None:
        owner = await robot.connect()
        watcher = await robot.connect()
        try:
            await _call(owner, "flipper.left", speed=0.5)
            await _call(owner, "servo.hold", id="right", us=1500)
            seq = robot.bus.latest_seq()
            await watcher.close()
            await _settled()
            assert robot.speed() == 0.5
            assert "right" in robot.servo.holding()
            assert robot.bus.history(since_seq=seq) == []
        finally:
            await owner.close()

    async def test_hold_takes_a_turning_flipper_from_its_owner(self, robot) -> None:
        # The hold replaces the flipper's pulses. A closing must not end B's hold,
        # and when B closes nothing may be left running.
        a = await robot.connect()
        b = await robot.connect()
        await _call(a, "flipper.left", speed=0.5)
        await _call(b, "servo.hold", id="left", us=1500)
        await a.close()
        await _settled()
        assert "left" in robot.servo.holding()
        await b.close()
        await _until(lambda: not robot.servo.holding() and robot.speed() == 0.0)

class TestCleanupIsRobust:
    async def test_one_failure_does_not_stop_the_others(self, robot, monkeypatch, caplog) -> None:
        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await _call(owner, "flipper.right", speed=0.7)
        await _call(owner, "servo.hold", id="m3", us=1500)

        async def broken():
            raise ValueError("servo line gone")

        monkeypatch.setattr(robot.peripherals.flipper_left, "stop", broken)
        await owner.close()
        await _until(lambda: robot.speed("right") == 0.0 and not robot.servo.holding())
        assert robot.speed("left") == 0.5
        assert "failed to stop flipper left" in caplog.text
        monkeypatch.undo()

    async def test_closing_while_the_start_is_still_waiting(self, robot) -> None:
        # The command is still queued on the flipper's lock when the socket goes.
        flipper = robot.peripherals.flipper_left
        owner = await robot.connect()
        await flipper._lock.acquire()
        await owner.send(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await _until(lambda: robot.peripherals.motion.owned() != {})
        owner.transport.abort()
        await asyncio.sleep(0.05)
        flipper._lock.release()
        await _settled()
        assert robot.speed() == 0.0
        assert await robot.servo.get_speed("left") == 0.0
        assert robot.peripherals.motion.owned() == {}

    async def test_slow_command_does_not_delay_the_stop(self, robot) -> None:
        # One command at a time per socket, so a long verb blocks the receive
        # loop. The motion it left running must stop anyway, and promptly.
        cancelled = asyncio.Event()

        @robot.dispatcher.verb("test.slow", args_model=_NoArgs)
        async def _slow(_args) -> dict:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {}

        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await owner.send(json.dumps({"type": "cmd", "id": "s", "verb": "test.slow"}))
        await asyncio.sleep(0.05)
        await owner.close()
        await _until(lambda: robot.speed() == 0.0, timeout=1.0)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    async def test_command_that_survives_cancellation_is_still_cleaned_up(self, robot) -> None:
        # Flipper.set_speed and friends swallow CancelledError while awaiting a
        # ramp, then carry on. Motion started after the first pass must not stick.
        @robot.dispatcher.verb("test.stubborn", args_model=_NoArgs)
        async def _stubborn(_args) -> dict:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(60)
            await robot.dispatcher.dispatch(
                Cmd(id="inner", verb="flipper.left", args={"speed": 0.5})
            )
            return {}

        owner = await robot.connect()
        await owner.send(json.dumps({"type": "cmd", "id": "s", "verb": "test.stubborn"}))
        await asyncio.sleep(0.05)
        await owner.close()
        await _settled()
        await _settled()
        assert robot.speed() == 0.0
        assert robot.peripherals.motion.owned() == {}

    async def test_command_slow_to_cancel_does_not_delay_the_stop(self, robot) -> None:
        finished = asyncio.Event()

        @robot.dispatcher.verb("test.slow_to_cancel", args_model=_NoArgs)
        async def _slow_to_cancel(_args) -> dict:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(1.5)
                finished.set()
                raise
            return {}

        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await owner.send(json.dumps({"type": "cmd", "id": "s", "verb": "test.slow_to_cancel"}))
        await asyncio.sleep(0.05)
        await owner.close()
        await _until(lambda: robot.speed() == 0.0, timeout=1.0)
        assert not finished.is_set()
        await asyncio.wait_for(finished.wait(), timeout=3.0)

    async def test_cancelled_after_the_servo_already_moved(self, robot, monkeypatch) -> None:
        # Cancellation can land after the hardware was written, for example in the
        # event publish. The claim has to stand or nothing would stop the flipper.
        flipper = robot.peripherals.flipper_left
        original = flipper.set_speed
        moved = asyncio.Event()

        async def moves_then_waits(speed, ramp_ms=0):
            await original(speed, ramp_ms=ramp_ms)
            moved.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(flipper, "set_speed", moves_then_waits)
        owner = await robot.connect()
        await owner.send(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await asyncio.wait_for(moved.wait(), timeout=2.0)
        await owner.close()
        await _until(lambda: robot.speed() == 0.0)
        assert robot.peripherals.motion.owned() == {}

    async def test_cancelled_connection_task(self, robot) -> None:
        stub = _StubConnection()
        task = asyncio.create_task(robot.server._json_control_loop(stub))
        stub.inbox.put_nowait(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await _until(lambda: robot.speed() == 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert robot.speed() == 0.0

    async def test_cancelled_twice_still_finishes_the_cleanup(self, robot) -> None:
        stub = _StubConnection()
        task = asyncio.create_task(robot.server._json_control_loop(stub))
        stub.inbox.put_nowait(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await _until(lambda: robot.speed() == 0.5)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: robot.speed() == 0.0)

    async def test_connection_task_that_crashes(self, robot, caplog) -> None:
        stub = _StubConnection()
        task = asyncio.create_task(robot.server._json_control_loop(stub))
        stub.inbox.put_nowait(
            json.dumps({"type": "cmd", "id": "x", "verb": "flipper.left", "args": {"speed": 0.5}})
        )
        await _until(lambda: robot.speed() == 0.5)
        stub.send_error = RuntimeError("transport fell over")
        # Sending the next ack raises something the loops don't expect.
        stub.inbox.put_nowait(json.dumps({"type": "cmd", "id": "y", "verb": "ping"}))
        await asyncio.wait_for(task, timeout=2.0)
        assert robot.speed() == 0.0
        assert "connection task failed" in caplog.text

    async def test_server_stop_stops_motion(self, robot) -> None:
        owner = await robot.connect()
        await _call(owner, "flipper.left", speed=0.5)
        await robot.server.stop()
        assert robot.speed() == 0.0


class _StubConnection:
    """Just enough of a ServerConnection to drive the control loop directly."""

    remote_address = ("stub", 0)

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.send_error: Exception | None = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self.inbox.get()

    async def send(self, _message: str) -> None:
        if self.send_error is not None:
            raise self.send_error

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


class TestKeepalive:
    @pytest.fixture
    async def fast_robot(self, tmp_path) -> AsyncIterator[_Robot]:
        dispatcher = Dispatcher()
        bus = EventBus()
        peripherals = await setup(dispatcher, bus, backend="fake", state=StateStore(tmp_path))
        server = Server(
            dispatcher, bus, TokenAuth(TOKEN), bind="127.0.0.1", port=0,
            motion=peripherals.motion,
            ping_interval=0.2, ping_timeout=0.2, close_timeout=0.2,
        )
        await server.start()
        port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            yield _Robot(dispatcher, bus, peripherals, server, port)
        finally:
            await server.stop()
            await peripherals.stop()

    async def test_defaults_reach_the_connection(self, robot) -> None:
        seen: list = []

        async def _handler(ws) -> None:
            seen.append((ws.ping_interval, ws.ping_timeout, ws.close_timeout))

        robot.server.add_path_handler("/probe", _handler)
        ws = await websockets.connect(
            f"ws://127.0.0.1:{robot.port}/probe", subprotocols=["bearer", TOKEN]
        )
        await _until(lambda: seen)
        await ws.close()
        assert seen == [(PING_INTERVAL_S, PING_TIMEOUT_S, CLOSE_TIMEOUT_S)]
        assert PING_INTERVAL_S + PING_TIMEOUT_S + CLOSE_TIMEOUT_S <= 12.0

    async def test_silent_peer_is_dropped_and_its_motion_stopped(self, fast_robot) -> None:
        # Stops reading, so no pongs: what Wi-Fi going away looks like from here.
        owner = await websockets.connect(
            f"ws://127.0.0.1:{fast_robot.port}",
            subprotocols=["bearer", TOKEN],
            ping_interval=None,
        )
        await _call(owner, "flipper.left", speed=0.5)
        owner.transport.pause_reading()
        await asyncio.sleep(0.1)
        assert fast_robot.speed() == 0.5
        await _until(lambda: fast_robot.speed() == 0.0, timeout=2.0)
        owner.transport.abort()

    async def test_quiet_healthy_peer_is_kept(self, fast_robot) -> None:
        owner = await fast_robot.connect()
        try:
            await _call(owner, "flipper.left", speed=0.5)
            await asyncio.sleep(1.0)
            assert fast_robot.speed() == 0.5
            assert (await _call(owner, "peripherals.status")).ok
        finally:
            await owner.close()

    async def test_busy_socket_is_not_mistaken_for_a_dead_one(self, fast_robot) -> None:
        # A slider dragged during a long verb. Frames pile up behind the running
        # command, and the pongs must still get read.
        @fast_robot.dispatcher.verb("test.slow", args_model=_NoArgs)
        async def _slow(_args) -> dict:
            await asyncio.sleep(1.5)
            return {"done": True}

        owner = await fast_robot.connect()
        try:
            await owner.send(json.dumps({"type": "cmd", "id": "s", "verb": "test.slow"}))
            # Just under the limit, and more than the library's default of 16.
            frames = MAX_QUEUED_FRAMES - 4
            assert frames > 16
            for i in range(frames):
                args = {"speed": 0.1}
                frame = {"type": "cmd", "id": f"f{i}", "verb": "flipper.left", "args": args}
                await owner.send(json.dumps(frame))
            acks = 0
            while acks < frames + 1:
                payload = json.loads(await asyncio.wait_for(owner.recv(), timeout=3.0))
                acks += payload["type"] == "ack"
            assert fast_robot.speed() == 0.1
        finally:
            await owner.close()
