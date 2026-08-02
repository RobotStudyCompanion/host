"""Behavioural tests for the fake HAL backends.

Each fake's contract — abstract methods + test hooks — is exercised here. The
real Pi backend (deferred) will be tested with a matching but smaller surface,
since hardware tests can't be driven as freely.
"""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.hal.fake import (
    FakeAudio,
    FakeGpioInput,
    FakeGpioPwm,
    FakeRing,
    FakeSerial,
    FakeServo,
)
from rsc_host.hal.types import Colour, Edge


# -----------------------------------------------------------------------------
# Servo
# -----------------------------------------------------------------------------


class TestFakeServo:
    async def test_lifecycle_idempotent(self) -> None:
        s = FakeServo()
        await s.start()
        await s.start()  # no second-init complaint
        await s.stop()
        await s.stop()  # safe to stop twice

    async def test_set_and_get(self) -> None:
        s = FakeServo()
        await s.start()
        await s.set_speed("left", 0.5)
        assert await s.get_speed("left") == 0.5

    async def test_get_unknown_returns_zero(self) -> None:
        s = FakeServo()
        assert await s.get_speed("never_set") == 0.0

    async def test_stop_all_zeros_known_servos(self) -> None:
        s = FakeServo()
        await s.set_speed("left", 0.7)
        await s.set_speed("right", -0.3)
        await s.stop_all()
        assert await s.get_speed("left") == 0.0
        assert await s.get_speed("right") == 0.0

    async def test_known_servos_tracks_set(self) -> None:
        s = FakeServo()
        await s.set_speed("left", 0.1)
        await s.set_speed("right", 0.2)
        assert set(s.known_servos()) == {"left", "right"}

    async def test_hal_does_not_validate_speed_range(self) -> None:
        # HAL accepts whatever; range validation belongs to the peripheral.
        s = FakeServo()
        await s.set_speed("left", 5.0)
        assert await s.get_speed("left") == 5.0


# -----------------------------------------------------------------------------
# Ring
# -----------------------------------------------------------------------------


class TestFakeRing:
    async def test_default_pixel_count(self) -> None:
        r = FakeRing()
        assert r.pixel_count == 16

    async def test_custom_pixel_count(self) -> None:
        r = FakeRing(pixel_count=24)
        assert r.pixel_count == 24

    async def test_rejects_zero_or_negative_pixel_count(self) -> None:
        with pytest.raises(ValueError):
            FakeRing(pixel_count=0)
        with pytest.raises(ValueError):
            FakeRing(pixel_count=-1)

    async def test_initial_frame_is_black(self) -> None:
        r = FakeRing(pixel_count=4)
        frame = await r.get_frame()
        assert frame == (Colour.black(),) * 4

    async def test_set_pixel_stages_but_does_not_show(self) -> None:
        r = FakeRing(pixel_count=4)
        await r.set_pixel(2, Colour(255, 0, 0))
        # Staged buffer has the red pixel; shown buffer still all black.
        assert r.staged_frame()[2] == Colour(255, 0, 0)
        assert (await r.get_frame())[2] == Colour.black()

    async def test_show_commits_staged_to_displayed(self) -> None:
        r = FakeRing(pixel_count=4)
        await r.set_pixel(0, Colour(255, 0, 0))
        await r.show()
        assert (await r.get_frame())[0] == Colour(255, 0, 0)

    async def test_fill_then_show(self) -> None:
        r = FakeRing(pixel_count=3)
        await r.fill(Colour(10, 20, 30))
        await r.show()
        assert await r.get_frame() == (Colour(10, 20, 30),) * 3

    async def test_set_pixel_out_of_range(self) -> None:
        r = FakeRing(pixel_count=4)
        with pytest.raises(IndexError):
            await r.set_pixel(4, Colour.white())
        with pytest.raises(IndexError):
            await r.set_pixel(-1, Colour.white())


# -----------------------------------------------------------------------------
# GPIO input
# -----------------------------------------------------------------------------


class TestFakeGpioInput:
    async def test_default_level_is_low(self) -> None:
        g = FakeGpioInput()
        assert await g.read(23) is False

    async def test_set_level(self) -> None:
        g = FakeGpioInput()
        g.set_level(23, True)
        assert await g.read(23) is True

    async def test_trigger_rising_sets_level_high(self) -> None:
        g = FakeGpioInput()
        g.trigger(23, Edge.RISING)
        assert await g.read(23) is True

    async def test_trigger_falling_sets_level_low(self) -> None:
        g = FakeGpioInput()
        g.set_level(23, True)
        g.trigger(23, Edge.FALLING)
        assert await g.read(23) is False

    async def test_edge_callback_invoked(self) -> None:
        g = FakeGpioInput()
        received: list[tuple[int, Edge]] = []

        def cb(event):  # noqa: ANN001
            received.append((event.pin, event.edge))

        await g.on_edge(23, cb)
        g.trigger(23, Edge.RISING)
        g.trigger(23, Edge.FALLING)
        assert received == [(23, Edge.RISING), (23, Edge.FALLING)]

    async def test_multiple_callbacks_fire_in_order(self) -> None:
        g = FakeGpioInput()
        order: list[str] = []
        await g.on_edge(23, lambda _e: order.append("first"))
        await g.on_edge(23, lambda _e: order.append("second"))
        g.trigger(23, Edge.RISING)
        assert order == ["first", "second"]

    async def test_callback_not_fired_for_other_pins(self) -> None:
        g = FakeGpioInput()
        received: list[int] = []
        await g.on_edge(23, lambda e: received.append(e.pin))
        g.trigger(24, Edge.RISING)
        assert received == []

    async def test_stop_clears_callbacks(self) -> None:
        g = FakeGpioInput()
        fired: list[int] = []
        await g.on_edge(23, lambda _e: fired.append(1))
        await g.stop()
        g.trigger(23, Edge.RISING)
        assert fired == []


# -----------------------------------------------------------------------------
# GPIO PWM output
# -----------------------------------------------------------------------------


class TestFakeGpioPwm:
    async def test_default_duty_is_zero(self) -> None:
        p = FakeGpioPwm()
        assert await p.get_duty(24) == 0.0

    async def test_set_then_get(self) -> None:
        p = FakeGpioPwm()
        await p.set_duty(24, 0.5)
        assert await p.get_duty(24) == 0.5

    async def test_history_records_every_set(self) -> None:
        p = FakeGpioPwm()
        for d in (0.1, 0.2, 0.5, 1.0):
            await p.set_duty(24, d)
        assert p.get_duty_history(24) == (0.1, 0.2, 0.5, 1.0)

    async def test_history_per_pin(self) -> None:
        p = FakeGpioPwm()
        await p.set_duty(24, 0.5)
        await p.set_duty(25, 0.7)
        assert p.get_duty_history(24) == (0.5,)
        assert p.get_duty_history(25) == (0.7,)


# -----------------------------------------------------------------------------
# Serial
# -----------------------------------------------------------------------------


class TestFakeSerial:
    async def test_write_then_inspect(self) -> None:
        s = FakeSerial()
        await s.write_line("mood:HAPPY")
        await s.write_line("theme:dark")
        assert s.written() == ("mood:HAPPY", "theme:dark")

    async def test_inject_then_read(self) -> None:
        s = FakeSerial()
        await s.inject("host_vol:42")
        line = await s.read_line()
        assert line == "host_vol:42"

    async def test_inject_many_preserves_order(self) -> None:
        s = FakeSerial()
        await s.inject_many(["host_vol:10", "host_mute", "host_vol:20"])
        first = await s.read_line()
        second = await s.read_line()
        third = await s.read_line()
        assert (first, second, third) == ("host_vol:10", "host_mute", "host_vol:20")

    async def test_read_blocks_until_inject(self) -> None:
        s = FakeSerial()
        # Start the read concurrently, then inject after a yield. The read
        # should resolve to the injected line, not raise or time out.
        read_task = asyncio.create_task(s.read_line())
        await asyncio.sleep(0)  # let read_task block on the queue
        assert not read_task.done()
        await s.inject("delayed")
        result = await asyncio.wait_for(read_task, timeout=1.0)
        assert result == "delayed"


# -----------------------------------------------------------------------------
# Audio
# -----------------------------------------------------------------------------


class TestFakeAudio:
    async def test_play_records_payload(self) -> None:
        a = FakeAudio()
        await a.play_wav(b"RIFF....")
        await a.play_wav(b"more")
        assert a.played() == (b"RIFF....", b"more")

    async def test_capture_lifecycle(self) -> None:
        a = FakeAudio()
        assert a.is_capturing() is False
        await a.start_capture(lambda _f: None)
        assert a.is_capturing() is True
        await a.stop_capture()
        assert a.is_capturing() is False

    async def test_emit_capture_routes_to_callback(self) -> None:
        a = FakeAudio()
        frames: list[bytes] = []
        await a.start_capture(frames.append)
        a.emit_capture(b"frame1")
        a.emit_capture(b"frame2")
        assert frames == [b"frame1", b"frame2"]

    async def test_emit_capture_no_op_when_not_running(self) -> None:
        a = FakeAudio()
        # Must not raise — emit_capture is safe to call regardless of state.
        a.emit_capture(b"orphan")

    async def test_stop_clears_callback(self) -> None:
        a = FakeAudio()
        frames: list[bytes] = []
        await a.start_capture(frames.append)
        await a.stop_capture()
        a.emit_capture(b"too_late")
        assert frames == []

    async def test_stop_capture_idempotent(self) -> None:
        a = FakeAudio()
        await a.stop_capture()  # safe even without prior start
        await a.start_capture(lambda _f: None)
        await a.stop_capture()
        await a.stop_capture()  # second stop also safe

    async def test_stream_pcm_records_chunks_and_params(self) -> None:
        a = FakeAudio()

        async def gen():
            yield b"aaa"
            yield b"bbb"
            yield b"cccc"

        await a.stream_pcm(gen(), samplerate=48000, channels=1, sample_width=2)
        streams = a.streamed()
        assert len(streams) == 1
        s = streams[0]
        assert s["samplerate"] == 48000
        assert s["channels"] == 1
        assert s["sample_width"] == 2
        assert s["chunks"] == [b"aaa", b"bbb", b"cccc"]
        assert s["total_bytes"] == 10

    async def test_stream_pcm_empty_iterator(self) -> None:
        a = FakeAudio()

        async def gen():
            if False:
                yield b""  # never runs

        await a.stream_pcm(gen(), samplerate=16000, channels=1)
        assert a.streamed()[0]["total_bytes"] == 0
