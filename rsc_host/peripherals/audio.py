"""Audio peripheral: playback + capture atop an :class:`AudioBackend`.

Wraps :class:`~rsc_host.hal.base.AudioBackend` with:

* **Playback lifecycle** — one clip at a time; overlapping ``play`` requests
  are rejected with a busy error (or cancel-then-play if the caller sets
  ``preempt=True``). Emits ``audio.play.started`` / ``audio.play.done``
  events so clients can synchronise gestures with speech.

* **Capture streaming** — one capture session at a time. Frames land in a
  per-session ``asyncio.Queue`` that the binary ``/audio/in`` endpoint drains
  and forwards. A session ends when the client disconnects or calls
  ``audio.capture.stop``.

The peripheral doesn't own the network transport — the server layer opens
``/audio/out`` and ``/audio/in`` and calls the methods here. Keeps the
peripheral testable against fake backends without any network.
"""
from __future__ import annotations

import asyncio
import logging

from rsc_host.events import EventBus
from rsc_host.hal.base import AudioBackend
from rsc_host.protocol import Event

log = logging.getLogger(__name__)


class PlaybackBusyError(RuntimeError):
    """Raised when a play request arrives while another is in progress."""


class CaptureBusyError(RuntimeError):
    """Raised when a capture session is requested while one is already active."""


class Audio:
    """Playback + capture coordinator atop an :class:`AudioBackend`."""

    def __init__(self, backend: AudioBackend, bus: EventBus) -> None:
        self._backend = backend
        self._bus = bus

        # Playback state
        self._play_task: asyncio.Task[None] | None = None
        self._play_lock = asyncio.Lock()

        # Capture state
        self._capture_queue: asyncio.Queue[bytes] | None = None
        self._capture_lock = asyncio.Lock()
        self._capture_loop: asyncio.AbstractEventLoop | None = None

    # ---- Playback ----

    @property
    def is_playing(self) -> bool:
        return self._play_task is not None and not self._play_task.done()

    async def play(self, wav_bytes: bytes, *, preempt: bool = False) -> None:
        """Play a WAV payload through the local audio sink.

        Awaits playback completion. Emits ``audio.play.started`` on entry
        and ``audio.play.done`` on completion (or cancellation).

        Args:
            wav_bytes: Complete WAV file bytes.
            preempt:   If True, cancel any in-flight playback first.
                       If False (default), raise :class:`PlaybackBusyError`
                       when another clip is playing.
        """
        async with self._play_lock:
            if self.is_playing:
                if not preempt:
                    raise PlaybackBusyError("another clip is currently playing")
                await self._cancel_play()

            byte_count = len(wav_bytes)
            await self._bus.publish(
                Event(
                    topic="audio.play.started",
                    source="host",
                    data={"bytes": byte_count},
                )
            )

            async def _run() -> None:
                try:
                    await self._backend.play_wav(wav_bytes)
                    outcome = "ok"
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception as exc:
                    log.exception("playback failed")
                    outcome = f"error: {type(exc).__name__}"
                finally:
                    await self._bus.publish(
                        Event(
                            topic="audio.play.done",
                            source="host",
                            data={"outcome": outcome, "bytes": byte_count},
                        )
                    )

            self._play_task = asyncio.create_task(_run(), name="audio-play")

        # Await outside the lock so subsequent play() calls block only on
        # lock acquisition, not on the previous clip's runtime.
        try:
            await self._play_task
        except asyncio.CancelledError:
            # play_task was cancelled by a preempting caller; that's fine.
            pass

    async def stream(
        self,
        pcm_chunks,
        *,
        samplerate: int,
        channels: int,
        sample_width: int = 2,
        preempt: bool = False,
    ) -> None:
        """Stream raw PCM chunks through the local sink with low latency.

        The first chunk starts playing as soon as it arrives — no full-buffer
        wait. The async iterator drives the pace; it can come from anywhere
        (a WebSocket, a generator, a TTS pipeline).

        Args:
            pcm_chunks:   async iterator yielding PCM byte chunks.
            samplerate:   Hz.
            channels:     1 or 2.
            sample_width: bytes per sample (default 2 = s16le).
            preempt:      cancel any in-flight playback first.

        Raises:
            PlaybackBusyError: if playback is active and ``preempt=False``.
        """
        async with self._play_lock:
            if self.is_playing:
                if not preempt:
                    raise PlaybackBusyError("another clip is currently playing")
                await self._cancel_play()

            await self._bus.publish(
                Event(
                    topic="audio.stream.started",
                    source="host",
                    data={
                        "samplerate": samplerate,
                        "channels": channels,
                        "sample_width": sample_width,
                    },
                )
            )

            async def _run() -> None:
                outcome = "ok"
                try:
                    await self._backend.stream_pcm(
                        pcm_chunks,
                        samplerate=samplerate,
                        channels=channels,
                        sample_width=sample_width,
                    )
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception as exc:
                    log.exception("stream failed")
                    outcome = f"error: {type(exc).__name__}"
                finally:
                    await self._bus.publish(
                        Event(
                            topic="audio.stream.done",
                            source="host",
                            data={"outcome": outcome},
                        )
                    )

            self._play_task = asyncio.create_task(_run(), name="audio-stream")

        try:
            await self._play_task
        except asyncio.CancelledError:
            pass

    async def stop_play(self) -> None:
        """Cancel any in-flight playback. Safe to call when nothing is playing."""
        async with self._play_lock:
            await self._cancel_play()

    async def _cancel_play(self) -> None:
        """Cancel and await the current play task, if any. Caller holds the lock."""
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()
            try:
                await self._play_task
            except (asyncio.CancelledError, Exception):
                pass
        self._play_task = None

    # ---- Capture ----

    @property
    def is_capturing(self) -> bool:
        return self._capture_queue is not None

    async def start_capture(self, queue_maxsize: int = 256) -> asyncio.Queue[bytes]:
        """Begin streaming captured audio into a queue for a subscriber to drain.

        Returns the queue. Callers ``await queue.get()`` for successive frames
        until they call :meth:`stop_capture` (or disconnect, at which point
        the server layer calls stop_capture on their behalf).

        Raises:
            CaptureBusyError: if a session is already active.
        """
        async with self._capture_lock:
            if self._capture_queue is not None:
                raise CaptureBusyError("capture session already active")

            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_maxsize)
            self._capture_queue = queue
            self._capture_loop = asyncio.get_running_loop()

            def _on_frame(frame: bytes) -> None:
                # Called on the loop thread (per HAL contract). Non-blocking put:
                # if the client is behind, drop rather than back-pressure the mic.
                q = self._capture_queue
                if q is None:
                    return
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    log.warning("capture queue full; dropping %d-byte frame", len(frame))

            await self._backend.start_capture(_on_frame)
            await self._bus.publish(
                Event(topic="audio.capture.started", source="host", data={})
            )
            return queue

    async def stop_capture(self) -> None:
        """End the current capture session. Idempotent."""
        async with self._capture_lock:
            if self._capture_queue is None:
                return
            await self._backend.stop_capture()
            # Wake any consumer blocked on queue.get() by putting a sentinel.
            # b"" is our end-of-stream marker; the /audio/in handler treats it
            # as "close the WebSocket cleanly".
            try:
                self._capture_queue.put_nowait(b"")
            except asyncio.QueueFull:
                pass
            self._capture_queue = None
            self._capture_loop = None
            await self._bus.publish(
                Event(topic="audio.capture.stopped", source="host", data={})
            )
