"""Fake HAL backends — in-memory, log-friendly, dependency-free.

Every fake implements the corresponding abstract base in :mod:`rsc_host.hal.base`
and *adds* test hooks for driving the fake from tests or laptop dev:

  * :class:`FakeServo` — get the full speed map, including unset servos
  * :class:`FakeRing` — get staged and shown frames separately
  * :class:`FakeGpioInput` — :meth:`trigger` synthesises edge events
  * :class:`FakeGpioPwm` — :meth:`get_duty_history` for trajectory inspection
  * :class:`FakeSerial` — :meth:`inject` for CYD-to-host lines,
    :meth:`written` for host-to-CYD lines
  * :class:`FakeAudio` — :meth:`played` for output payloads,
    :meth:`emit_capture` for synthesising captured frames

All fakes are safe to ``start``/``stop`` repeatedly; the lifecycle is mostly a
running-flag toggle plus a log line.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Iterable

from rsc_host.hal.base import (
    AudioBackend,
    AudioCallback,
    GpioCallback,
    GpioInputBackend,
    GpioPwmBackend,
    RingBackend,
    SerialBackend,
    ServoBackend,
)
from rsc_host.hal.types import Colour, Edge, GpioEdge

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Servo
# -----------------------------------------------------------------------------


class FakeServo(ServoBackend):
    """In-memory servo backend.

    Stores last-set speed per servo_id. ``stop_all`` zeroes every entry it has
    seen, which means servos registered but never moved will still be set to 0.
    """

    def __init__(self) -> None:
        self._speeds: dict[str, float] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("FakeServo started")

    async def stop(self) -> None:
        self._running = False
        log.info("FakeServo stopped")

    async def set_speed(self, servo_id: str, speed: float) -> None:
        self._speeds[servo_id] = speed
        log.debug("FakeServo[%s] speed=%.3f", servo_id, speed)

    async def get_speed(self, servo_id: str) -> float:
        return self._speeds.get(servo_id, 0.0)

    async def stop_all(self) -> None:
        for servo_id in self._speeds:
            self._speeds[servo_id] = 0.0
        log.info("FakeServo stop_all (%d servos)", len(self._speeds))

    # ---- Test hooks ----

    def known_servos(self) -> tuple[str, ...]:
        """Servo IDs that have been touched by :meth:`set_speed` at least once."""
        return tuple(self._speeds.keys())


# -----------------------------------------------------------------------------
# Ring
# -----------------------------------------------------------------------------


class FakeRing(RingBackend):
    """In-memory NeoPixel ring backend.

    Maintains *two* buffers: ``_staged`` (writes via set_pixel / fill go here)
    and ``_shown`` (snapshot taken on each :meth:`show` call). Mirrors the real
    backend's commit-on-show semantics and lets tests assert what's currently
    displayed vs what's been queued up.
    """

    def __init__(self, pixel_count: int = 16) -> None:
        if pixel_count <= 0:
            raise ValueError(f"pixel_count must be > 0, got {pixel_count}")
        self._pixel_count = pixel_count
        self._staged: list[Colour] = [Colour.black()] * pixel_count
        self._shown: list[Colour] = [Colour.black()] * pixel_count
        self._running = False

    @property
    def pixel_count(self) -> int:
        return self._pixel_count

    async def start(self) -> None:
        self._running = True
        log.info("FakeRing started (%d pixels)", self._pixel_count)

    async def stop(self) -> None:
        self._running = False
        log.info("FakeRing stopped")

    async def set_pixel(self, index: int, colour: Colour) -> None:
        if not 0 <= index < self._pixel_count:
            raise IndexError(
                f"pixel index {index} out of range [0, {self._pixel_count})"
            )
        self._staged[index] = colour

    async def fill(self, colour: Colour) -> None:
        for i in range(self._pixel_count):
            self._staged[i] = colour

    async def show(self) -> None:
        self._shown = list(self._staged)
        log.debug("FakeRing show: %s", self._shown[0] if self._shown else "<empty>")

    async def get_frame(self) -> tuple[Colour, ...]:
        return tuple(self._shown)

    # ---- Test hooks ----

    def staged_frame(self) -> tuple[Colour, ...]:
        """Currently-staged frame (not yet committed via :meth:`show`)."""
        return tuple(self._staged)


# -----------------------------------------------------------------------------
# GPIO input
# -----------------------------------------------------------------------------


class FakeGpioInput(GpioInputBackend):
    """In-memory digital input backend.

    Tests drive edges via :meth:`trigger`. Levels and edges are kept consistent
    automatically: triggering RISING sets the level high, FALLING sets it low.
    """

    def __init__(self) -> None:
        self._levels: dict[int, bool] = {}
        self._callbacks: dict[int, list[GpioCallback]] = defaultdict(list)
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("FakeGpioInput started")

    async def stop(self) -> None:
        self._running = False
        self._callbacks.clear()
        log.info("FakeGpioInput stopped")

    async def read(self, pin: int) -> bool:
        return self._levels.get(pin, False)

    async def on_edge(self, pin: int, callback: GpioCallback) -> None:
        self._callbacks[pin].append(callback)

    # ---- Test hooks ----

    def trigger(self, pin: int, edge: Edge) -> None:
        """Synthesise an edge event on ``pin``. Fires every registered callback.

        Synchronous on purpose — callers can call this from sync test bodies
        without juggling event loops. The callbacks themselves are invoked
        synchronously; if a callback schedules async work, that work runs on
        the next loop iteration.
        """
        self._levels[pin] = edge == Edge.RISING
        event = GpioEdge(pin=pin, edge=edge, timestamp_ns=time.monotonic_ns())
        for cb in self._callbacks.get(pin, ()):
            cb(event)

    def set_level(self, pin: int, level: bool) -> None:
        """Set the static level on ``pin`` without firing edge callbacks.
        Useful for tests that poll :meth:`read` rather than subscribe to edges."""
        self._levels[pin] = level


# -----------------------------------------------------------------------------
# GPIO PWM output
# -----------------------------------------------------------------------------


class FakeGpioPwm(GpioPwmBackend):
    """In-memory PWM output backend.

    Records duty history per pin so tests can inspect trajectories
    (e.g. verify a fade ramped through the expected intermediate values).
    """

    def __init__(self) -> None:
        self._duties: dict[int, float] = {}
        self._history: dict[int, list[float]] = defaultdict(list)
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("FakeGpioPwm started")

    async def stop(self) -> None:
        self._running = False
        log.info("FakeGpioPwm stopped")

    async def set_duty(self, pin: int, duty: float) -> None:
        self._duties[pin] = duty
        self._history[pin].append(duty)
        log.debug("FakeGpioPwm[pin=%d] duty=%.3f", pin, duty)

    async def get_duty(self, pin: int) -> float:
        return self._duties.get(pin, 0.0)

    # ---- Test hooks ----

    def get_duty_history(self, pin: int) -> tuple[float, ...]:
        """All duty values ever set on ``pin``, in order. Empty if never touched."""
        return tuple(self._history.get(pin, ()))


# -----------------------------------------------------------------------------
# Serial
# -----------------------------------------------------------------------------


class FakeSerial(SerialBackend):
    """In-memory bidirectional serial backend.

    Host-to-CYD writes accumulate in ``_tx``; CYD-to-host lines are pushed via
    :meth:`inject` and pulled by :meth:`read_line`. The fake is good enough
    to exercise the CYD bridge end-to-end in unit tests without a real UART
    or even a socat loopback.
    """

    def __init__(self) -> None:
        self._tx: list[str] = []
        self._rx: asyncio.Queue[str] = asyncio.Queue()
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("FakeSerial started")

    async def stop(self) -> None:
        self._running = False
        log.info("FakeSerial stopped")

    async def write_line(self, line: str) -> None:
        self._tx.append(line)
        log.debug("FakeSerial tx: %r", line)

    async def read_line(self) -> str:
        return await self._rx.get()

    # ---- Test hooks ----

    def written(self) -> tuple[str, ...]:
        """All lines the host has written so far, in order."""
        return tuple(self._tx)

    async def inject(self, line: str) -> None:
        """Simulate the CYD sending ``line`` to the host."""
        await self._rx.put(line)

    async def inject_many(self, lines: Iterable[str]) -> None:
        """Convenience: inject several lines in order."""
        for line in lines:
            await self._rx.put(line)


# -----------------------------------------------------------------------------
# Audio
# -----------------------------------------------------------------------------


class FakeAudio(AudioBackend):
    """In-memory audio backend.

    :meth:`play_wav` records the payload and returns immediately — the fake
    does not simulate playback duration. Tests that care about timing should
    use their own clock; this backend exists to verify *what* was played, not
    *when* it finished.
    """

    def __init__(self) -> None:
        self._played: list[bytes] = []
        self._capture_callback: AudioCallback | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("FakeAudio started")

    async def stop(self) -> None:
        self._running = False
        self._capture_callback = None
        log.info("FakeAudio stopped")

    async def play_wav(self, wav_bytes: bytes) -> None:
        self._played.append(wav_bytes)
        log.debug("FakeAudio play_wav: %d bytes", len(wav_bytes))

    async def start_capture(self, callback: AudioCallback) -> None:
        self._capture_callback = callback

    async def stop_capture(self) -> None:
        self._capture_callback = None

    # ---- Test hooks ----

    def played(self) -> tuple[bytes, ...]:
        """All payloads that have been played, in order."""
        return tuple(self._played)

    def is_capturing(self) -> bool:
        return self._capture_callback is not None

    def emit_capture(self, frame: bytes) -> None:
        """Synthesise a captured audio frame to the current callback.
        No-op if capture isn't running."""
        if self._capture_callback is not None:
            self._capture_callback(frame)
