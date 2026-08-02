"""ArcadeButton peripheral: edge → event, debouncing, press/release direction."""
from __future__ import annotations

import asyncio
import time

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeGpioInput
from rsc_host.hal.types import Edge, GpioEdge
from rsc_host.peripherals.button import ArcadeButton


@pytest.fixture
async def rig():
    backend = FakeGpioInput()
    await backend.start()
    bus = EventBus()
    button = ArcadeButton(pin=23, backend=backend, bus=bus)
    await button.start()
    try:
        yield button, backend, bus
    finally:
        await backend.stop()


class TestEdgeMapping:
    async def test_rising_publishes_button_press(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "button.press"
        assert evt.source == "host"
        assert evt.data == {"pin": 23}

    async def test_falling_publishes_button_release(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            backend.set_level(23, True)
            backend.trigger(23, Edge.FALLING)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "button.release"
        assert evt.data == {"pin": 23}


class TestDebounce:
    async def test_rapid_double_rising_debounced(self, rig) -> None:
        _, backend, bus = rig
        # Bypass FakeGpioInput.trigger's monotonic timestamp and inject two
        # rising edges within the 20ms guard window manually. Easier: call
        # trigger twice back-to-back; monotonic_ns between calls is usually
        # well under 20ms.
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            backend.trigger(23, Edge.RISING)
            # First should arrive; second should be debounced out.
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
            assert evt.topic == "button.press"
            # No further event within a short wait.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_rising_after_debounce_window_accepted(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            await q.get()
            # Wait past the 20ms guard.
            await asyncio.sleep(0.03)
            backend.trigger(23, Edge.RISING)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
            assert evt.topic == "button.press"

    async def test_press_and_release_not_debounced_against_each_other(self, rig) -> None:
        _, backend, bus = rig
        # RISING and FALLING have independent debounce state; a press
        # immediately followed by a release should both fire.
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            backend.trigger(23, Edge.FALLING)
            e1 = await asyncio.wait_for(q.get(), timeout=1.0)
            e2 = await asyncio.wait_for(q.get(), timeout=1.0)
            topics = {e1.topic, e2.topic}
            assert topics == {"button.press", "button.release"}
