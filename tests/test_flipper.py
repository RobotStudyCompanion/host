"""Flipper peripheral: speed validation, ramping, events, cancellation, M3 disable."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeServo
from rsc_host.peripherals.flipper import Flipper


@pytest.fixture
async def rig():
    backend = FakeServo()
    await backend.start()
    bus = EventBus()
    flipper = Flipper("left", backend, bus)
    try:
        yield flipper, backend, bus
    finally:
        await flipper.stop()
        await backend.stop()


class TestValidation:
    async def test_speed_in_range_accepted(self, rig) -> None:
        flipper, _, _ = rig
        await flipper.set_speed(0.5)
        assert flipper.current_speed == 0.5

    async def test_speed_out_of_range_rejected(self, rig) -> None:
        flipper, _, _ = rig
        with pytest.raises(ValueError, match=r"\[-1\.0, 1\.0\]"):
            await flipper.set_speed(1.5)
        with pytest.raises(ValueError):
            await flipper.set_speed(-2.0)

    async def test_negative_ramp_rejected(self, rig) -> None:
        flipper, _, _ = rig
        with pytest.raises(ValueError, match="ramp_ms"):
            await flipper.set_speed(0.0, ramp_ms=-1)


class TestImmediateSet:
    async def test_writes_to_backend(self, rig) -> None:
        flipper, backend, _ = rig
        await flipper.set_speed(0.75)
        assert await backend.get_speed("left") == 0.75

    async def test_emits_state_event(self, rig) -> None:
        flipper, _, bus = rig
        async with bus.subscribe() as q:
            await flipper.set_speed(0.3)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "flipper.left.state"
        assert evt.data["speed"] == 0.3
        assert evt.data["enabled"] is True


class TestRamping:
    async def test_ramp_reaches_target(self, rig) -> None:
        flipper, backend, _ = rig
        await flipper.set_speed(1.0, ramp_ms=50)
        # Let the ramp finish.
        await asyncio.sleep(0.15)
        # Final backend value should be at or near target.
        assert abs(await backend.get_speed("left") - 1.0) < 1e-6

    async def test_ramp_emits_intermediate_events(self, rig) -> None:
        flipper, _, bus = rig
        async with bus.subscribe() as q:
            await flipper.set_speed(1.0, ramp_ms=50)
            await asyncio.sleep(0.15)
            events = []
            while not q.empty():
                events.append(q.get_nowait())
        # At 100 Hz, a 50ms ramp emits ~5 events; require at least 3 to guard
        # against timing jitter but still verify ramping is happening.
        assert len(events) >= 3
        # Last event should be the final target.
        assert events[-1].data["speed"] == pytest.approx(1.0, abs=1e-6)

    async def test_new_set_cancels_in_flight_ramp(self, rig) -> None:
        flipper, backend, _ = rig
        # Kick off a long ramp
        await flipper.set_speed(1.0, ramp_ms=500)
        await asyncio.sleep(0.05)
        # Preempt with an immediate opposite direction
        await flipper.set_speed(-0.5, ramp_ms=0)
        await asyncio.sleep(0.02)
        # Backend should reflect the immediate call, not the ramp target.
        assert await backend.get_speed("left") == -0.5

    async def test_stop_kills_ramp(self, rig) -> None:
        flipper, backend, _ = rig
        await flipper.set_speed(1.0, ramp_ms=500)
        await asyncio.sleep(0.05)
        await flipper.stop()
        assert await backend.get_speed("left") == 0.0


class TestDisabled:
    async def test_m3_disabled_does_not_touch_backend(self) -> None:
        backend = FakeServo()
        await backend.start()
        bus = EventBus()
        flipper = Flipper("m3", backend, bus, enabled=False)
        try:
            await flipper.set_speed(0.5)
            # Backend never touched — m3 not in known_servos.
            assert "m3" not in backend.known_servos()
            # Event still emitted; state tracked internally.
            assert flipper.current_speed == 0.5
        finally:
            await flipper.stop()
            await backend.stop()

    async def test_m3_disabled_event_marks_enabled_false(self) -> None:
        backend = FakeServo()
        await backend.start()
        bus = EventBus()
        flipper = Flipper("m3", backend, bus, enabled=False)
        try:
            async with bus.subscribe() as q:
                await flipper.set_speed(0.5)
                evt = await asyncio.wait_for(q.get(), timeout=1.0)
            assert evt.data["enabled"] is False
        finally:
            await flipper.stop()
            await backend.stop()


class _FailsOnce(FakeServo):
    """Raises on the ``n``-th set_speed call, then works again."""

    def __init__(self, n: int, *, forever: bool = False) -> None:
        super().__init__()
        self._n = n
        self._forever = forever
        self.calls = 0

    async def set_speed(self, servo_id: str, speed: float) -> None:
        self.calls += 1
        if self.calls == self._n or (self._forever and self.calls > self._n):
            raise OSError("servo bus fell over")
        await super().set_speed(servo_id, speed)


class TestRampFailure:
    """Step 2b, H4: a ramp that dies part-way must not leave the servo turning."""

    async def _ramp_and_settle(self, backend) -> tuple[Flipper, list]:
        await backend.start()
        bus = EventBus()
        flipper = Flipper("left", backend, bus)
        async with bus.subscribe() as q:
            await flipper.set_speed(1.0, ramp_ms=300)
            await asyncio.sleep(0.6)
            events = []
            while not q.empty():
                events.append(q.get_nowait())
        return flipper, events

    async def test_backend_error_mid_ramp_ends_at_zero(self, caplog) -> None:
        backend = _FailsOnce(10)
        flipper, events = await self._ramp_and_settle(backend)
        assert await backend.get_speed("left") == 0.0
        assert flipper.current_speed == 0.0
        assert events[-1].data["speed"] == 0.0
        assert "ramp failed" in caplog.text
        # The ramp stopped at the failure rather than carrying on to 1.0.
        assert backend.calls == 11

    async def test_backend_that_stays_broken_still_publishes(self) -> None:
        backend = _FailsOnce(10, forever=True)
        flipper, events = await self._ramp_and_settle(backend)
        # Nothing reached the hardware, but the state says what was attempted.
        assert flipper.current_speed == 0.0
        assert events[-1].data["speed"] == 0.0
