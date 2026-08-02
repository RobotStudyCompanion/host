"""Registry integration: setup() wires HAL + peripherals + verbs together."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeGpioInput
from rsc_host.hal.types import Edge
from rsc_host.peripherals.registry import Pinout, setup


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
        assert peripherals.serial_backend.written() == ("mood:HAPPY",)

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
