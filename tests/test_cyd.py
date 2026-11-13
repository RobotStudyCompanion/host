"""CydBridge: reader ingest, curated writer, raw escape hatch, malformed handling."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeSerial
from rsc_host.peripherals.cyd import MOODS, CydBridge, curated_cyd_verbs


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


# ---- Screen state (step 2, sync) ----

SENTINEL = "host_vol:1"


async def _events_until_sentinel(backend, q, *lines: str) -> list:
    """Inject ``lines`` then a sentinel; return every event before it.

    The reader handles lines in order, so once the sentinel's event arrives
    every earlier line has been fully processed, published events included.
    """
    await backend.inject_many([*lines, SENTINEL])
    seen = []
    while True:
        evt = await asyncio.wait_for(q.get(), timeout=1.0)
        if evt.topic == "host_vol" and evt.data == {"value": 1}:
            return seen
        seen.append(evt)


def _states(events) -> list[dict]:
    return [e.data for e in events if e.topic == "cyd.state"]


class TestReportLines:
    async def test_host_face_publishes_state_once(self, rig) -> None:
        cyd, backend, bus = rig
        async with bus.subscribe() as q:
            first = await _events_until_sentinel(backend, q, "host_face:HAPPY")
            again = await _events_until_sentinel(backend, q, "host_face:HAPPY")
        assert [e.topic for e in first] == ["host_face", "cyd.state"]
        assert first[0].source == "cyd" and first[0].data == {"value": "HAPPY"}
        assert first[1].source == "host"
        assert first[1].data == {
            "mood": "HAPPY", "cycle": None, "bright": None,
            "auto_bright": None, "reports": True,
        }
        # The cyd event is still published; the state did not change.
        assert [e.topic for e in again] == ["host_face"]
        assert cyd.snapshot()["mood"] == "HAPPY"

    @pytest.mark.parametrize(
        "line, field, value",
        [
            ("host_face:SAD", "mood", "SAD"),
            ("host_face:IDLE2", "mood", "IDLE2"),
            ("host_face:CUSTOM", "mood", "CUSTOM"),
            ("host_cycle:on", "cycle", True),
            ("host_cycle:off", "cycle", False),
            ("host_bright:40", "bright", 40),
            ("host_auto_bright:on", "auto_bright", True),
            ("host_auto_bright:off", "auto_bright", False),
        ],
    )
    async def test_each_report_line_sets_its_field(
        self, rig, line, field, value
    ) -> None:
        cyd, backend, bus = rig
        assert cyd.reports is False
        async with bus.subscribe() as q:
            events = await _events_until_sentinel(backend, q, line)
        expected = {
            "mood": None, "cycle": None, "bright": None,
            "auto_bright": None, "reports": True, field: value,
        }
        assert _states(events) == [expected]
        assert cyd.snapshot() == expected
        assert cyd.reports is True

    @pytest.mark.parametrize(
        "line",
        [
            "host_bright:abc",
            "host_bright:101",
            "host_face:SMILE",
            "host_cycle:maybe",
            "host_auto_bright:",
        ],
    )
    async def test_malformed_report_logged_and_dropped(
        self, rig, line, caplog
    ) -> None:
        cyd, backend, bus = rig
        async with bus.subscribe() as q:
            events = await _events_until_sentinel(backend, q, line)
        assert events == []
        assert cyd.snapshot()["reports"] is False
        assert any("malformed" in r.message for r in caplog.records)

    async def test_no_report_line_writes_to_the_screen(self, rig) -> None:
        _, backend, bus = rig
        async with bus.subscribe() as q:
            await _events_until_sentinel(
                backend, q,
                "host_ready", "host_face:HAPPY", "host_cycle:on",
                "host_bright:30", "host_auto_bright:on",
            )
        assert backend.written() == ()

    async def test_host_ready_turns_push_and_reports_on(self, rig) -> None:
        cyd, backend, bus = rig
        assert cyd.supports_push is False
        async with bus.subscribe() as q:
            events = await _events_until_sentinel(backend, q, "host_ready")
        assert [e.topic for e in events] == ["host_ready", "cyd.state"]
        assert events[0].source == "cyd"
        assert cyd.supports_push is True
        assert cyd.reports is True


class TestSentValuesRecorded:
    async def test_mood_recorded_then_matching_report_is_quiet(self, rig) -> None:
        cyd, backend, bus = rig
        async with bus.subscribe() as q:
            assert await cyd.send_curated("cyd.mood", "happy") == "HAPPY"
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
            assert evt.topic == "cyd.state"
            assert evt.data["mood"] == "HAPPY"
            assert evt.data["reports"] is False
            events = await _events_until_sentinel(backend, q, "host_face:HAPPY")
        assert backend.written() == ("mood:HAPPY",)
        # Only reports flips; the mood was already known.
        assert _states(events) == [{**evt.data, "reports": True}]

    async def test_mood_cycle_recorded(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.mood_cycle", "on")
        assert cyd.snapshot()["cycle"] is True
        await cyd.send_curated("cyd.mood_cycle", "OFF")
        assert cyd.snapshot()["cycle"] is False
        assert backend.written() == ("mood_cycle:on", "mood_cycle:off")

    async def test_bright_recorded_and_turns_auto_off(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.bright", " 040 ")
        assert backend.written() == ("bright:40",)
        assert cyd.snapshot()["bright"] == 40
        assert cyd.snapshot()["auto_bright"] is False

    async def test_face_recorded_as_custom(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.face", "0,0,0,30,45")
        assert backend.written() == ("face:0,0,0,30,45",)
        assert cyd.snapshot()["mood"] == "CUSTOM"

    async def test_unchanged_value_publishes_nothing(self, rig) -> None:
        cyd, _, bus = rig
        await cyd.send_curated("cyd.mood", "SAD")
        async with bus.subscribe() as q:
            await cyd.send_curated("cyd.mood", "SAD")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_untracked_verbs_leave_state_alone(self, rig) -> None:
        cyd, _, _ = rig
        before = cyd.snapshot()
        await cyd.send_curated("cyd.blink", None)
        await cyd.send_raw("mood:HAPPY")
        assert cyd.snapshot() == before

    async def test_failed_write_records_nothing(self, rig) -> None:
        cyd, backend, _ = rig

        async def broken(line: str) -> None:
            raise OSError("unplugged")

        backend.write_line = broken
        with pytest.raises(OSError):
            await cyd.send_curated("cyd.mood", "HAPPY")
        assert cyd.snapshot()["mood"] is None


class TestValidation:
    @pytest.mark.parametrize(
        "verb, value",
        [
            ("cyd.mood", "SMILE"),
            ("cyd.mood", ""),
            ("cyd.mood", None),
            ("cyd.mood", "CUSTOM"),
            ("cyd.mood_cycle", "yes"),
            ("cyd.mood_cycle", None),
            ("cyd.bright", "0"),
            ("cyd.bright", "101"),
            ("cyd.bright", "abc"),
            ("cyd.bright", "4_0"),
            ("cyd.bright", "+40"),
            ("cyd.bright", "40.5"),
            ("cyd.bright", None),
        ],
    )
    async def test_rejected_and_nothing_written(self, rig, verb, value) -> None:
        cyd, backend, _ = rig
        with pytest.raises(ValueError):
            await cyd.send_curated(verb, value)
        assert backend.written() == ()
        assert cyd.snapshot()["mood"] is None

    @pytest.mark.parametrize("name", MOODS)
    async def test_every_mood_accepted_in_any_case(self, rig, name) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.mood", name.lower())
        assert backend.written() == (f"mood:{name}",)

    @pytest.mark.parametrize("value", ["1", "100"])
    async def test_bright_bounds_accepted(self, rig, value) -> None:
        cyd, backend, _ = rig
        await cyd.send_curated("cyd.bright", value)
        assert backend.written() == (f"bright:{value}",)


class TestScreenLines:
    async def test_push_state_uses_mic_mute(self, rig) -> None:
        cyd, backend, _ = rig
        cyd._supports_push = True
        await cyd.push_state(volume=40, muted=False, mic_muted=True)
        assert backend.written() == ("vol:40", "mute:off", "mic_mute:on")

    async def test_old_firmware(self, rig) -> None:
        cyd, backend, _ = rig
        await cyd.screen_on()
        await cyd.screen_off()
        await cyd.request_report()
        assert backend.written() == ("resume", "pause", "clear", "led:off", "report")

    async def test_new_firmware(self, rig) -> None:
        cyd, backend, _ = rig
        cyd._supports_push = True
        await cyd.screen_on()
        await cyd.screen_off()
        await cyd.request_report()
        assert backend.written() == ("screen:on", "screen:off", "report")

    async def test_write_failure_is_not_raised(self, rig) -> None:
        cyd, backend, _ = rig

        async def broken(line: str) -> None:
            raise OSError("unplugged")

        backend.write_line = broken
        await cyd.screen_off()
        await cyd.request_report()
