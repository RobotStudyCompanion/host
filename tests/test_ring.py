"""Ring peripheral: mode registry, switching, cancellation, events."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeRing
from rsc_host.hal.types import Colour
from rsc_host.peripherals.ring import Ring, registered_modes


@pytest.fixture
async def rig():
    backend = FakeRing(pixel_count=16)
    await backend.start()
    bus = EventBus()
    ring = Ring(backend, bus)
    try:
        yield ring, backend, bus
    finally:
        await ring.stop()
        await backend.stop()


class TestRegistry:
    def test_builtin_modes_present(self) -> None:
        modes = set(registered_modes())
        assert {"off", "solid", "pulse", "spin", "sweep"} <= modes


class TestModeSwitching:
    async def test_solid_sets_frame(self, rig) -> None:
        ring, backend, _ = rig
        await ring.set_mode("solid", {"r": 255, "g": 0, "b": 0})
        # Let the mode task write at least one frame.
        await asyncio.sleep(0.05)
        frame = await backend.get_frame()
        assert frame[0] == Colour(255, 0, 0)

    async def test_off_clears(self, rig) -> None:
        ring, backend, _ = rig
        await ring.set_mode("solid", {"r": 255, "g": 255, "b": 255})
        await asyncio.sleep(0.05)
        await ring.set_mode("off")
        await asyncio.sleep(0.05)
        frame = await backend.get_frame()
        assert all(px == Colour.black() for px in frame)

    async def test_unknown_mode_raises(self, rig) -> None:
        ring, _, _ = rig
        with pytest.raises(KeyError, match="unknown ring mode"):
            await ring.set_mode("does_not_exist")

    async def test_mode_switch_cancels_previous(self, rig) -> None:
        ring, backend, _ = rig
        await ring.set_mode("spin", {"period_ms": 200})
        await asyncio.sleep(0.05)
        await ring.set_mode("solid", {"r": 0, "g": 255, "b": 0})
        # After the switch, the spin task shouldn't be overwriting our solid green.
        await asyncio.sleep(0.1)
        frame = await backend.get_frame()
        # All pixels should be green (spin would have varied).
        assert all(px == Colour(0, 255, 0) for px in frame)

    async def test_current_mode_tracks(self, rig) -> None:
        ring, _, _ = rig
        assert ring.current_mode is None
        await ring.set_mode("solid", {"r": 1, "g": 2, "b": 3})
        assert ring.current_mode == "solid"
        await ring.set_mode("off")
        assert ring.current_mode == "off"


class TestEvents:
    async def test_emits_mode_event(self, rig) -> None:
        ring, _, bus = rig
        async with bus.subscribe() as q:
            await ring.set_mode("solid", {"r": 10, "g": 20, "b": 30})
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "ring.mode"
        assert evt.data["mode"] == "solid"
        assert evt.data["params"] == {"r": 10, "g": 20, "b": 30}
