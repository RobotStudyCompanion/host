"""Ring peripheral: mode registry, switching, cancellation, events."""
from __future__ import annotations

import asyncio
import re

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


NAN = float("nan")
INF = float("inf")

BAD_PARAMS = [
    # period_ms
    {"period_ms": 0},
    {"period_ms": 49},
    {"period_ms": 49.9},
    {"period_ms": 60_001},
    {"period_ms": -100},
    {"period_ms": "800"},
    {"period_ms": True},
    {"period_ms": None},
    {"period_ms": NAN},
    {"period_ms": INF},
    {"period_ms": -INF},
    {"period_ms": 10**400},
    # r, g, b
    {"r": 256, "g": 0, "b": 0},
    {"r": 0, "g": -1, "b": 0},
    {"r": 0, "g": 0, "b": 255.5},
    {"r": "255", "g": 0, "b": 0},
    {"r": True, "g": 0, "b": 0},
    {"r": None, "g": 0, "b": 0},
    {"r": NAN, "g": 0, "b": 0},
    {"r": 0, "g": INF, "b": 0},
    # A part that is present is checked even when the rest are missing.
    {"r": 300},
    {"g": "green"},
    # rgb
    {"rgb": [1, 2]},
    {"rgb": [1, 2, 3, 4]},
    {"rgb": []},
    {"rgb": "abc"},
    {"rgb": 5},
    {"rgb": None},
    {"rgb": {"r": 1, "g": 2, "b": 3}},
    {"rgb": [1, 2, 300]},
    {"rgb": [-1, 2, 3]},
    {"rgb": ["1", 2, 3]},
    {"rgb": [True, 2, 3]},
    {"rgb": [NAN, 2, 3]},
    {"rgb": [1, INF, 3]},
    # Checked whichever one the mode would read.
    {"rgb": [1, 2, 3], "r": 300},
]


class TestParamValidation:
    @pytest.mark.parametrize("params", BAD_PARAMS, ids=repr)
    @pytest.mark.parametrize("mode", ["solid", "pulse", "spin", "sweep", "off"])
    async def test_rejected_and_running_pattern_untouched(self, rig, mode, params) -> None:
        ring, backend, bus = rig
        await ring.set_mode("pulse", {"r": 0, "g": 255, "b": 0, "period_ms": 200})
        task = ring._task
        async with bus.subscribe() as q:
            with pytest.raises(ValueError):
                await ring.set_mode(mode, params)
            assert q.empty()
        assert ring._task is task and not task.done()
        assert ring.current_mode == "pulse"
        assert ring._current_params == {"r": 0, "g": 255, "b": 0, "period_ms": 200}
        # Still animating, not just still present.
        await asyncio.sleep(0.05)
        first = await backend.get_frame()
        await asyncio.sleep(0.05)
        assert await backend.get_frame() != first

    @pytest.mark.parametrize("rgb", [[1, 2], [1, 2, 3, 4], [], [1]], ids=repr)
    async def test_wrong_length_rgb_says_so(self, rig, rgb) -> None:
        ring, _, _ = rig
        message = f"rgb: must be a list of exactly 3 numbers, got {rgb!r}"
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            await ring.set_mode("solid", {"rgb": rgb})

    async def test_unknown_mode_leaves_pattern_running(self, rig) -> None:
        ring, _, _ = rig
        await ring.set_mode("spin", {"period_ms": 200})
        task = ring._task
        with pytest.raises(KeyError):
            await ring.set_mode("does_not_exist")
        assert ring._task is task and not task.done()
        assert ring.current_mode == "spin"

    @pytest.mark.parametrize("mode", ["off", "solid", "pulse", "spin", "sweep"])
    async def test_every_mode_works_without_params(self, rig, mode) -> None:
        ring, _, _ = rig
        await ring.set_mode(mode)
        await asyncio.sleep(0.05)
        assert ring.current_mode == mode
        assert not ring._task.done()

    @pytest.mark.parametrize("period_ms", [50, 50.0, 800, 1500, 4000, 60_000])
    @pytest.mark.parametrize("mode", ["pulse", "spin", "sweep"])
    async def test_period_limits_accepted_and_run(self, rig, mode, period_ms) -> None:
        ring, _, _ = rig
        await ring.set_mode(mode, {"period_ms": period_ms})
        await asyncio.sleep(0.05)
        assert not ring._task.done()

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({}, Colour(255, 255, 255)),
            ({"r": 1, "g": 2, "b": 3}, Colour(1, 2, 3)),
            ({"r": 0, "g": 0, "b": 255}, Colour(0, 0, 255)),
            ({"rgb": [4, 5, 6]}, Colour(4, 5, 6)),
            # Fractions keep truncating, as they always have.
            ({"r": 1.9, "g": 2.0, "b": 3}, Colour(1, 2, 3)),
            # rgb wins when both are given.
            ({"rgb": [7, 8, 9], "r": 1, "g": 1, "b": 1}, Colour(7, 8, 9)),
            # Missing parts keep falling back to the default colour.
            ({"r": 10}, Colour(255, 255, 255)),
            ({"r": 10, "g": 20}, Colour(255, 255, 255)),
            # Fields this layer doesn't know about keep being ignored.
            ({"r": 1, "g": 2, "b": 3, "note": "anything"}, Colour(1, 2, 3)),
        ],
        ids=repr,
    )
    async def test_valid_colours_behave_as_before(self, rig, params, expected) -> None:
        ring, backend, _ = rig
        await ring.set_mode("solid", params)
        await asyncio.sleep(0.05)
        assert (await backend.get_frame())[0] == expected

    async def test_valid_switch_after_a_rejection_still_works(self, rig) -> None:
        ring, backend, _ = rig
        await ring.set_mode("pulse", {"period_ms": 200})
        with pytest.raises(ValueError):
            await ring.set_mode("pulse", {"period_ms": 0})
        await ring.set_mode("solid", {"r": 9, "g": 9, "b": 9})
        await asyncio.sleep(0.05)
        assert (await backend.get_frame())[0] == Colour(9, 9, 9)
