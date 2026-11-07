"""ButtonLed peripheral: mode switching, param validation, events."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeGpioPwm
from rsc_host.peripherals.button_led import ButtonLed


@pytest.fixture
async def rig():
    backend = FakeGpioPwm()
    await backend.start()
    bus = EventBus()
    led = ButtonLed(pin=24, backend=backend, bus=bus)
    try:
        yield led, backend, bus
    finally:
        await led.stop()
        await backend.stop()


class TestModes:
    async def test_off_sets_duty_zero(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("off")
        assert await backend.get_duty(24) == 0.0

    async def test_on_default_duty(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("on")
        assert await backend.get_duty(24) == 1.0

    async def test_on_custom_duty(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("on", {"duty": 0.3})
        assert await backend.get_duty(24) == 0.3

    async def test_on_duty_out_of_range_rejected(self, rig) -> None:
        led, _, _ = rig
        with pytest.raises(ValueError, match="duty"):
            await led.set_mode("on", {"duty": 1.5})

    async def test_pulse_writes_multiple_duties(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("pulse", {"period_ms": 100})
        await asyncio.sleep(0.15)
        history = backend.get_duty_history(24)
        # Multiple frames written during the pulse.
        assert len(history) >= 3

    async def test_unknown_mode_rejected(self, rig) -> None:
        led, _, _ = rig
        with pytest.raises(KeyError, match="unknown button LED mode"):
            await led.set_mode("does_not_exist")

    async def test_mode_switch_cancels_animation(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("pulse", {"period_ms": 200})
        await asyncio.sleep(0.05)
        await led.set_mode("off")
        # After switching to off, further writes should stop.
        await asyncio.sleep(0.05)
        len_after_off = len(backend.get_duty_history(24))
        await asyncio.sleep(0.1)
        assert len(backend.get_duty_history(24)) == len_after_off


class TestEvents:
    async def test_emits_state_event(self, rig) -> None:
        led, _, bus = rig
        async with bus.subscribe() as q:
            await led.set_mode("on", {"duty": 0.7})
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "button_led.state"
        assert evt.data["mode"] == "on"
        assert evt.data["duty"] == 0.7


NAN = float("nan")
INF = float("inf")

BAD_PARAMS = [
    {"duty": -0.1},
    {"duty": 1.1},
    {"duty": 2},
    {"duty": "0.5"},
    {"duty": True},
    {"duty": None},
    {"duty": NAN},
    {"duty": INF},
    {"duty": -INF},
    {"period_ms": 0},
    {"period_ms": 49},
    {"period_ms": 60_001},
    {"period_ms": -100},
    {"period_ms": "800"},
    {"period_ms": True},
    {"period_ms": None},
    {"period_ms": NAN},
    {"period_ms": INF},
]


class TestParamValidation:
    @pytest.mark.parametrize("params", BAD_PARAMS, ids=repr)
    @pytest.mark.parametrize("mode", ["off", "on", "pulse", "breathe"])
    async def test_rejected_and_running_pattern_untouched(self, rig, mode, params) -> None:
        led, backend, bus = rig
        await led.set_mode("breathe", {"period_ms": 200})
        task = led._task
        async with bus.subscribe() as q:
            with pytest.raises(ValueError):
                await led.set_mode(mode, params)
            assert q.empty()
        assert led._task is task and not task.done()
        assert led.current_mode == "breathe"
        # Still animating, not just still present.
        before = len(backend.get_duty_history(24))
        await asyncio.sleep(0.1)
        assert len(backend.get_duty_history(24)) > before

    async def test_unknown_mode_leaves_pattern_running(self, rig) -> None:
        led, _, bus = rig
        await led.set_mode("pulse", {"period_ms": 200})
        task = led._task
        async with bus.subscribe() as q:
            with pytest.raises(KeyError, match="unknown button LED mode"):
                await led.set_mode("does_not_exist")
            assert q.empty()
        assert led._task is task and not task.done()
        assert led.current_mode == "pulse"

    async def test_bad_duty_leaves_steady_mode_alone(self, rig) -> None:
        led, backend, _ = rig
        await led.set_mode("on", {"duty": 0.4})
        with pytest.raises(ValueError):
            await led.set_mode("on", {"duty": 1.5})
        assert led.current_mode == "on"
        assert await backend.get_duty(24) == 0.4

    @pytest.mark.parametrize("mode", ["off", "on", "pulse", "breathe"])
    async def test_every_mode_works_without_params(self, rig, mode) -> None:
        led, _, _ = rig
        await led.set_mode(mode)
        await asyncio.sleep(0.05)
        assert led.current_mode == mode
        assert led._task is None or not led._task.done()

    @pytest.mark.parametrize("period_ms", [50, 100, 800, 4000, 60_000, 800.5])
    @pytest.mark.parametrize("mode", ["pulse", "breathe"])
    async def test_period_limits_accepted_and_run(self, rig, mode, period_ms) -> None:
        led, _, _ = rig
        await led.set_mode(mode, {"period_ms": period_ms})
        await asyncio.sleep(0.05)
        assert not led._task.done()

    @pytest.mark.parametrize("duty", [0, 0.0, 0.5, 1, 1.0])
    async def test_duty_limits_accepted(self, rig, duty) -> None:
        led, backend, bus = rig
        async with bus.subscribe() as q:
            await led.set_mode("on", {"duty": duty})
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert await backend.get_duty(24) == float(duty)
        assert evt.data["duty"] == float(duty)
