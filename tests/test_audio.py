"""Audio peripheral: playback lifecycle, capture queue, events, busy errors."""
from __future__ import annotations

import asyncio

import pytest

from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeAudio
from rsc_host.peripherals.audio import Audio, CaptureBusyError, PlaybackBusyError


@pytest.fixture
async def rig():
    backend = FakeAudio()
    await backend.start()
    bus = EventBus()
    audio = Audio(backend, bus)
    try:
        yield audio, backend, bus
    finally:
        await audio.stop_capture()
        await audio.stop_play()
        await backend.stop()


class TestPlayback:
    async def test_play_writes_to_backend(self, rig) -> None:
        audio, backend, _ = rig
        await audio.play(b"RIFF....wavdata")
        assert backend.played() == (b"RIFF....wavdata",)

    async def test_play_emits_started_and_done(self, rig) -> None:
        audio, _, bus = rig
        async with bus.subscribe() as q:
            await audio.play(b"abcd")
            events = []
            while not q.empty():
                events.append(q.get_nowait())
        topics = [e.topic for e in events]
        assert "audio.play.started" in topics
        assert "audio.play.done" in topics

    async def test_started_event_carries_byte_count(self, rig) -> None:
        audio, _, bus = rig
        async with bus.subscribe() as q:
            await audio.play(b"abcdefghij")
            first = await q.get()
        assert first.topic == "audio.play.started"
        assert first.data == {"bytes": 10}

    async def test_done_event_marks_outcome_ok(self, rig) -> None:
        audio, _, bus = rig
        async with bus.subscribe() as q:
            await audio.play(b"x")
            await q.get()  # started
            done = await q.get()
        assert done.topic == "audio.play.done"
        assert done.data["outcome"] == "ok"


class TestPlaybackBusy:
    async def test_second_play_raises_when_not_preempted(self, rig) -> None:
        # We can't easily hold play open on the fake (returns instantly), so
        # simulate a slow backend inline.
        audio, backend, _ = rig

        slow_start = asyncio.Event()
        slow_release = asyncio.Event()

        async def slow_play(_wav: bytes) -> None:
            slow_start.set()
            await slow_release.wait()

        backend.play_wav = slow_play  # type: ignore[assignment]

        first = asyncio.create_task(audio.play(b"first"))
        await slow_start.wait()

        with pytest.raises(PlaybackBusyError):
            await audio.play(b"second")

        slow_release.set()
        await first

    async def test_preempt_cancels_previous(self, rig) -> None:
        audio, backend, _ = rig

        slow_start = asyncio.Event()
        slow_release = asyncio.Event()

        async def slow_play(_wav: bytes) -> None:
            slow_start.set()
            await slow_release.wait()

        backend.play_wav = slow_play  # type: ignore[assignment]

        first = asyncio.create_task(audio.play(b"first"))
        await slow_start.wait()

        # Replace slow_play with a fast one for the preempting call.
        played_second: list[bytes] = []

        async def fast_play(wav: bytes) -> None:
            played_second.append(wav)

        backend.play_wav = fast_play  # type: ignore[assignment]

        await audio.play(b"second", preempt=True)
        assert played_second == [b"second"]

        slow_release.set()
        try:
            await first
        except (asyncio.CancelledError, Exception):
            pass


class TestCapture:
    async def test_start_and_stop_lifecycle(self, rig) -> None:
        audio, backend, _ = rig
        assert audio.is_capturing is False
        queue = await audio.start_capture()
        assert audio.is_capturing is True
        assert backend.is_capturing() is True
        await audio.stop_capture()
        assert audio.is_capturing is False

    async def test_second_start_raises(self, rig) -> None:
        audio, _, _ = rig
        await audio.start_capture()
        with pytest.raises(CaptureBusyError):
            await audio.start_capture()

    async def test_frames_arrive_on_queue(self, rig) -> None:
        audio, backend, _ = rig
        queue = await audio.start_capture()
        backend.emit_capture(b"frame1")
        backend.emit_capture(b"frame2")
        f1 = await asyncio.wait_for(queue.get(), timeout=1.0)
        f2 = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert (f1, f2) == (b"frame1", b"frame2")

    async def test_stop_capture_sends_sentinel(self, rig) -> None:
        audio, backend, _ = rig
        queue = await audio.start_capture()
        backend.emit_capture(b"data")
        await audio.stop_capture()
        # We should see the frame then the empty sentinel.
        got = []
        while not queue.empty():
            got.append(queue.get_nowait())
        assert b"data" in got
        assert b"" in got  # sentinel

    async def test_stop_when_not_capturing_is_noop(self, rig) -> None:
        audio, _, _ = rig
        await audio.stop_capture()  # no raise, no state change
        assert audio.is_capturing is False

    async def test_capture_events(self, rig) -> None:
        audio, _, bus = rig
        async with bus.subscribe() as q:
            await audio.start_capture()
            e1 = await asyncio.wait_for(q.get(), timeout=1.0)
            await audio.stop_capture()
            e2 = await asyncio.wait_for(q.get(), timeout=1.0)
        assert e1.topic == "audio.capture.started"
        assert e2.topic == "audio.capture.stopped"

    async def test_queue_overflow_drops_frames_not_backpressures(self, rig) -> None:
        # A slow client shouldn't stall the mic. Small queue, overflow it.
        audio, backend, _ = rig
        queue = await audio.start_capture(queue_maxsize=2)
        for i in range(10):
            backend.emit_capture(f"frame-{i}".encode())
        # Queue never grows past its maxsize; extras dropped.
        assert queue.qsize() <= 2


class TestStream:
    async def test_stream_forwards_to_backend(self, rig) -> None:
        audio, backend, _ = rig

        async def gen():
            yield b"chunk-1"
            yield b"chunk-2"

        await audio.stream(gen(), samplerate=48000, channels=1)
        streams = backend.streamed()
        assert len(streams) == 1
        assert streams[0]["samplerate"] == 48000
        assert streams[0]["channels"] == 1
        assert streams[0]["chunks"] == [b"chunk-1", b"chunk-2"]

    async def test_stream_emits_started_and_done(self, rig) -> None:
        audio, _, bus = rig

        async def gen():
            yield b"data"

        async with bus.subscribe() as q:
            await audio.stream(gen(), samplerate=16000, channels=1)
            events = []
            while not q.empty():
                events.append(q.get_nowait())
        topics = [e.topic for e in events]
        assert "audio.stream.started" in topics
        assert "audio.stream.done" in topics

    async def test_stream_started_carries_format(self, rig) -> None:
        audio, _, bus = rig

        async def gen():
            yield b"x"

        async with bus.subscribe() as q:
            await audio.stream(gen(), samplerate=44100, channels=2)
            e = await asyncio.wait_for(q.get(), timeout=1.0)
        assert e.topic == "audio.stream.started"
        assert e.data["samplerate"] == 44100
        assert e.data["channels"] == 2

    async def test_stream_busy_raises_without_preempt(self, rig) -> None:
        audio, backend, _ = rig

        slow_start = asyncio.Event()
        slow_release = asyncio.Event()

        async def slow_stream(chunks, **_kw) -> None:
            slow_start.set()
            await slow_release.wait()

        backend.stream_pcm = slow_stream  # type: ignore[assignment]

        async def gen():
            yield b"x"

        first = asyncio.create_task(audio.stream(gen(), samplerate=16000, channels=1))
        await slow_start.wait()

        async def gen2():
            yield b"y"

        with pytest.raises(PlaybackBusyError):
            await audio.stream(gen2(), samplerate=16000, channels=1)

        slow_release.set()
        await first
