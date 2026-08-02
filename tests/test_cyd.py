"""CydBridge: reader ingest, curated writer, raw escape hatch, malformed handling."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeSerial
from rsc_host.peripherals.cyd import CydBridge, curated_cyd_verbs


@pytest.fixture
async def rig():
    backend = FakeSerial()
    await backend.start()
    bus = EventBus()
    cyd = CydBridge(backend, bus)
    await cyd.start()
    try:
        yield cyd, backend, bus
    finally:
        await cyd.stop()
        await backend.stop()


class TestReader:
    async def test_host_vol_publishes_event(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("host_vol:42")
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "host_vol"
        assert evt.source == "cyd"
        assert evt.data == {"value": 42}

    async def test_host_mute_publishes_event(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("host_mute")
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "host_mute"
        assert evt.source == "cyd"
        assert evt.data == {}

    async def test_host_poweroff_ingested(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("host_poweroff")
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "host_poweroff"

    async def test_unknown_line_ignored(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("some_random_line:whatever")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_malformed_int_dropped(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("host_vol:not-a-number")
            # Malformed → dropped, no event.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_empty_line_ignored(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await backend.inject("")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)


class TestWriter:
    async def test_curated_verb_with_value(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.mood", "HAPPY")
        assert backend.written() == ("mood:HAPPY",)

    async def test_curated_verb_without_value(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.blink", None)
        assert backend.written() == ("blink",)

    async def test_unknown_verb_raises(self, rig) -> None:
        cyd, _, _ = rig
        with pytest.raises(KeyError):
            await cyd.send_curated("cyd.not_a_real_verb", "x")

    async def test_raw_sends_verbatim(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_raw("custom_line:with:colons")
        assert backend.written() == ("custom_line:with:colons",)


class TestCuratedList:
    def test_expected_verbs_present(self) -> None:
        verbs = set(curated_cyd_verbs())
        expected = {
            "cyd.mood", "cyd.theme", "cyd.bright", "cyd.eye_colour",
            "cyd.bg_colour", "cyd.led", "cyd.blink", "cyd.splash",
            "cyd.face", "cyd.look", "cyd.mood_cycle",
        }
        assert expected <= verbs
