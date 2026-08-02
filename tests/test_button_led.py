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
