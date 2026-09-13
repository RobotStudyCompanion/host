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

    _DEFAULT_CAL = {"null_us": 1500, "span_us": 100, "invert": False,
                    "min_us": 900, "max_us": 2100}

    def __init__(self) -> None:
        self._speeds: dict[str, float] = {}
        self._cal: dict[str, dict] = {}
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

    def calibration(self, servo_id: str | None = None) -> dict:
        if servo_id is not None:
            return {servo_id: dict(self._cal.get(servo_id, self._DEFAULT_CAL))}
        known = set(self._cal) | set(self._speeds) | {"left", "right", "m3"}
        return {sid: dict(self._cal.get(sid, self._DEFAULT_CAL)) for sid in sorted(known)}

    def set_calibration(self, servo_id: str, **changes) -> dict:
        unknown = set(changes) - set(self._DEFAULT_CAL)
        if unknown:
            raise ValueError(f"unknown calibration fields: {sorted(unknown)}")
        current = dict(self._cal.get(servo_id, self._DEFAULT_CAL))
        current.update(changes)
        if not current["min_us"] < current["null_us"] < current["max_us"]:
            raise ValueError(f"{servo_id}: null_us outside the pulse window")
        if current["span_us"] <= 0:
            raise ValueError(f"{servo_id}: span_us must be > 0")
        self._cal[servo_id] = current
        return dict(current)

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
        self._streamed: list[dict] = []
        self._capture_callback: AudioCallback | None = None
        self._running = False
        # Capture chain parameters, mirroring the Pi backend so the tuning
        # verbs can be exercised on a laptop. The chain itself is built lazily
        # (it needs numpy) and only when a frame is actually pushed through.
        self._capture_config: object | None = None
        self._chain: object | None = None
        self._mixer: dict[str, str] = {
            "ALC Function": "Off",
            "ADC High Pass Filter": "on",
            "Left Input Boost Mixer LINPUT1": "3",
            "Capture": "35",
            "Playback": "255",
        }
        self._devices: dict = {
            "input":  [{"index": 0, "name": "Fake Mic",     "channels": 1, "samplerate": 16000}],
            "output": [{"index": 0, "name": "Fake Speaker", "channels": 2, "samplerate": 48000}],
            "default_input":  0,
            "default_output": 0,
        }

    async def list_devices(self) -> dict:
        return dict(self._devices)

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

    async def stream_pcm(
        self,
        pcm_chunks,
        *,
        samplerate: int,
        channels: int,
        sample_width: int = 2,
    ) -> None:
        # Accumulate chunks for test inspection; capture the format params.
        chunks: list[bytes] = []
        async for chunk in pcm_chunks:
            chunks.append(chunk)
        self._streamed.append(
            {
                "samplerate": samplerate,
                "channels": channels,
                "sample_width": sample_width,
                "chunks": chunks,
                "total_bytes": sum(len(c) for c in chunks),
            }
        )
        log.debug(
            "FakeAudio stream_pcm: %d chunks / %d bytes @ %d Hz",
            len(chunks), sum(len(c) for c in chunks), samplerate,
        )

    async def start_capture(self, callback: AudioCallback) -> None:
        self._capture_callback = callback
        self._chain = None  # rebuilt on the first frame

    async def stop_capture(self) -> None:
        self._capture_callback = None
        self._chain = None

    # ---- Optional capabilities ----

    def _config(self):
        """Lazily construct the default capture config (needs numpy)."""
        from rsc_host.hal.dsp import CaptureConfig

        if self._capture_config is None:
            self._capture_config = CaptureConfig()
        return self._capture_config

    def capture_config(self) -> dict:
        cfg = self._config()
        out = cfg.as_dict()
        out["capturing"] = self._capture_callback is not None
        out["valid_stream_rates"] = list(
            type(cfg).valid_stream_rates(cfg.device_rate)
        )
        return out

    def retune_capture(self, **changes) -> dict:
        from dataclasses import replace

        self._capture_config = replace(self._config(), **changes)  # validates
        self._chain = None
        return self.capture_config()

    def capture_stats(self) -> dict:
        if self._chain is None:
            return {"capturing": self._capture_callback is not None,
                    "config": self._config().as_dict()}
        stats = self._chain.stats()  # type: ignore[attr-defined]
        stats["capturing"] = True
        return stats

    def reset_capture_stats(self) -> None:
        if self._chain is not None:
            self._chain.reset_stats()  # type: ignore[attr-defined]

    async def mixer_get(self, names=None) -> dict:
        wanted = tuple(names) if names is not None else tuple(self._mixer)
        return {"card": "fake",
                "controls": {n: self._mixer.get(n) for n in wanted}}

    async def mixer_set(self, name: str, value: str) -> dict:
        self._mixer[name] = value
        return {"control": name, "value": value}

    async def mixer_apply_preset(self) -> dict:
        return {name: "ok" for name in self._mixer}

    async def mixer_store(self, path: str = "/var/lib/alsa/asound.state") -> dict:
        return {"stored": True, "path": path}

    # ---- Test hooks ----

    def played(self) -> tuple[bytes, ...]:
        """All payloads that have been played, in order."""
        return tuple(self._played)

    def streamed(self) -> tuple[dict, ...]:
        """All PCM streams that have been submitted, in order.

        Each entry: ``{samplerate, channels, sample_width, chunks, total_bytes}``.
        """
        return tuple(self._streamed)

    def inject_devices(self, devices: dict) -> None:
        """Test hook: override the device list returned by :meth:`list_devices`."""
        self._devices = dict(devices)

    def is_capturing(self) -> bool:
        return self._capture_callback is not None

    def emit_capture(self, frame: bytes, *, process: bool = False) -> None:
        """Synthesise a captured audio frame to the current callback.

        No-op if capture isn't running. With ``process=True`` the frame is run
        through the real :class:`~rsc_host.hal.dsp.CaptureChain` first, so a
        test can drive the same DSP the hardware backend uses — pass raw
        interleaved device-rate bytes in that case.
        """
        if self._capture_callback is None:
            return
        if process:
            from rsc_host.hal.dsp import CaptureChain

            if self._chain is None:
                self._chain = CaptureChain(self._config())
            frame = self._chain.process(frame)  # type: ignore[attr-defined]
        if frame:
            self._capture_callback(frame)
