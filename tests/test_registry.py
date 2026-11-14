"""Registry integration: setup() wires HAL + peripherals + verbs together."""
from __future__ import annotations

import asyncio
import json

import pytest

from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeGpioInput
from rsc_host.hal.types import Edge
from rsc_host.peripherals.registry import Pinout, setup
from rsc_host.protocol import Cmd
from rsc_host.state import StateStore
from tests.test_button_led import BAD_PARAMS as LED_BAD_PARAMS
from tests.test_ring import BAD_PARAMS as RING_BAD_PARAMS


@pytest.fixture
async def rig():
    dispatcher = Dispatcher()
    bus = EventBus()
    peripherals = await setup(dispatcher, bus, backend="fake")
    try:
        yield dispatcher, bus, peripherals
    finally:
        await peripherals.stop()


class TestVerbRegistration:
    async def test_flipper_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        for side in ("left", "right", "m3"):
            assert f"flipper.{side}" in verbs
            assert f"flipper.{side}.stop" in verbs

    async def test_ring_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "ring.mode" in verbs
        assert "ring.modes" in verbs

    async def test_button_led_verb_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        assert "button_led" in dispatcher.registered_verbs()

    async def test_cyd_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "cyd.mood" in verbs
        assert "cyd.raw" in verbs

    async def test_audio_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "audio.play_url" in verbs
        assert "audio.stop_play" in verbs
        assert "audio.capture.stop" in verbs
        assert "audio.devices" in verbs


class TestEndToEnd:
    async def test_flipper_verb_moves_backend(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        cmd = Cmd(id="c1", verb="flipper.left", args={"speed": 0.5})
        ack = await dispatcher.dispatch(cmd)
        assert ack.ok is True
        assert await peripherals.servo_backend.get_speed("left") == 0.5

    async def test_ring_verb_lists_modes(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, _ = rig
        ack = await dispatcher.dispatch(Cmd(id="c2", verb="ring.modes", args={}))
        assert ack.ok is True
        assert "solid" in ack.result["modes"]

    async def test_cyd_curated_writes_serial(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        ack = await dispatcher.dispatch(
            Cmd(id="c3", verb="cyd.mood", args={"value": "HAPPY"})
        )
        assert ack.ok is True
        # Setup also sends the "vol?" capability probe; ignore it here.
        written = [
            line for line in peripherals.serial_backend.written() if line != "vol?"
        ]
        assert written == ["mood:HAPPY"]

    async def test_button_edge_fires_event(self, rig) -> None:
        _, bus, peripherals = rig
        backend = peripherals.gpio_in_backend
        assert isinstance(backend, FakeGpioInput)
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "button.press"

    async def test_cyd_line_becomes_event(self, rig) -> None:
        from rsc_host.hal.fake import FakeSerial
        _, bus, peripherals = rig
        serial = peripherals.serial_backend
        assert isinstance(serial, FakeSerial)
        async with bus.subscribe() as q:
            await serial.inject("host_vol:88")
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "host_vol"
        assert evt.data == {"value": 88}

    async def test_audio_devices_returns_shape(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, _ = rig
        ack = await dispatcher.dispatch(Cmd(id="cd", verb="audio.devices", args={}))
        assert ack.ok is True
        assert "input" in ack.result and "output" in ack.result
        assert "default_input" in ack.result and "default_output" in ack.result


class TestM3Disabled:
    async def test_m3_verb_succeeds_but_backend_untouched(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        ack = await dispatcher.dispatch(
            Cmd(id="c4", verb="flipper.m3", args={"speed": 0.5})
        )
        assert ack.ok is True
        assert ack.result["enabled"] is False
        # Backend never saw an m3 speed set.
        assert "m3" not in peripherals.servo_backend.known_servos()


class TestBadParamsOnTheWire:
    """Bad ring and LED params come back as an error ack, not a crash afterwards."""

    @pytest.fixture
    async def wired(self, tmp_path):
        dispatcher = Dispatcher()
        bus = EventBus()
        peripherals = await setup(dispatcher, bus, backend="fake", state=StateStore(tmp_path))
        try:
            yield dispatcher, bus, peripherals
        finally:
            await peripherals.stop()

    # One rig per test rather than per value: setup() waits on the panel probe.
    async def test_ring(self, wired) -> None:
        dispatcher, bus, peripherals = wired
        running = await dispatcher.dispatch(
            Cmd(id="a", verb="ring.mode", args={"mode": "pulse", "params": {"period_ms": 200}})
        )
        assert running.ok
        task = peripherals.ring._task
        seq = bus.latest_seq()
        for params in RING_BAD_PARAMS:
            ack = await dispatcher.dispatch(
                Cmd(id="b", verb="ring.mode", args={"mode": "spin", "params": params})
            )
            assert ack.ok is False, params
            assert ack.code == "INVALID_ARGS", params
            assert peripherals.ring._task is task and not task.done(), params
            assert peripherals.ring.current_mode == "pulse", params
        assert bus.history(since_seq=seq) == []

    async def test_button_led(self, wired) -> None:
        dispatcher, bus, peripherals = wired
        running = await dispatcher.dispatch(
            Cmd(id="a", verb="button_led", args={"mode": "breathe", "params": {"period_ms": 800}})
        )
        assert running.ok
        task = peripherals.button_led._task
        seq = bus.latest_seq()
        for params in LED_BAD_PARAMS:
            ack = await dispatcher.dispatch(
                Cmd(id="b", verb="button_led", args={"mode": "pulse", "params": params})
            )
            assert ack.ok is False, params
            assert ack.code == "INVALID_ARGS", params
            assert peripherals.button_led._task is task and not task.done(), params
            assert peripherals.button_led.current_mode == "breathe", params
        assert bus.history(since_seq=seq) == []

    @pytest.mark.parametrize(
        ("verb", "mode"),
        [("ring.mode", "does_not_exist"), ("button_led", "does_not_exist")],
    )
    async def test_unknown_mode(self, wired, verb, mode) -> None:
        dispatcher, _, _ = wired
        ack = await dispatcher.dispatch(Cmd(id="a", verb=verb, args={"mode": mode}))
        assert ack.code == "INVALID_ARGS"

    async def test_valid_commands_still_work(self, wired) -> None:
        dispatcher, _, peripherals = wired
        commands = [
            ("ring.mode", {"mode": "off"}),
            ("ring.mode", {"mode": "solid"}),
            ("ring.mode", {"mode": "pulse"}),
            ("ring.mode", {"mode": "spin"}),
            # What the console sends today.
            (
                "ring.mode",
                {"mode": "pulse", "params": {"r": 10, "g": 20, "b": 30, "period_ms": 1500}},
            ),
            ("button_led", {"mode": "off"}),
            ("button_led", {"mode": "on"}),
            ("button_led", {"mode": "pulse"}),
            ("button_led", {"mode": "breathe"}),
            ("button_led", {"mode": "pulse", "params": {"period_ms": 800}}),
            ("button_led", {"mode": "breathe", "params": {"period_ms": 4000}}),
            ("button_led", {"mode": "on", "params": {"duty": 0.3}}),
        ]
        for verb, args in commands:
            ack = await dispatcher.dispatch(Cmd(id="a", verb=verb, args=args))
            assert ack.ok is True, args
            assert ack.result["mode"] == args["mode"], args
            await asyncio.sleep(0.05)
            peripheral = peripherals.ring if verb == "ring.mode" else peripherals.button_led
            assert peripheral.current_mode == args["mode"], args
            assert peripheral._task is None or not peripheral._task.done(), args

    async def test_nan_arriving_as_json_is_rejected(self, wired) -> None:
        # json.loads accepts NaN, so this is reachable from a real client.
        dispatcher, _, _ = wired
        payload = json.loads('{"type":"cmd","id":"a","verb":"button_led",'
                             '"args":{"mode":"on","params":{"duty":NaN}}}')
        ack = await dispatcher.dispatch(Cmd.model_validate(payload))
        assert ack.code == "INVALID_ARGS"
