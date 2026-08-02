"""Hardware abstraction base classes.

One abstract class per peripheral *type*; concrete backends (fake, pi) implement
all of them. Domain peripherals (Flipper, Ring, ArcadeButton, ...) compose
backend instances and add semantic logic — ramping, debouncing, mode machines.

Async-first across the board:

  * **Outputs and queries** are coroutines. Fake implementations return
    immediately; real implementations may bridge blocking hardware libs via
    :func:`asyncio.to_thread` or threadsafe loop scheduling.
  * **Input events** (GPIO edges, captured audio frames) are delivered via
    callback registration. Backends guarantee callbacks fire on the asyncio
    event loop thread, so it's safe to schedule asyncio work from inside them.

Lifecycle: every backend exposes ``start()`` and ``stop()``. The server calls
``start`` once at boot, ``stop`` once on shutdown. Backends own their resources
in between; peripherals don't need to know about init order.
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterable, Callable

from rsc_host.hal.types import Colour, GpioEdge


class Backend(abc.ABC):
    """Common lifecycle for any HAL backend."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Initialise hardware / open file handles. Idempotent."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Release hardware / close handles. Idempotent. Safe after start failure."""


# -----------------------------------------------------------------------------
# Servos
# -----------------------------------------------------------------------------


class ServoBackend(Backend):
    """Continuous-rotation servo control.

    Speed is a normalised float: **−1.0** = full reverse, **0.0** = stop,
    **+1.0** = full forward. The real backend maps to pulse width
    (e.g. 1000..2000 µs) with per-servo calibration; the abstraction stays in
    speed-space so callers don't depend on hardware timing.

    Ramping is *not* a HAL concern. Peripheral wrappers (e.g. ``Flipper``) call
    :meth:`set_speed` repeatedly at 50–200 Hz to interpolate between speeds.
    """

    @abc.abstractmethod
    async def set_speed(self, servo_id: str, speed: float) -> None:
        """Set ``servo_id`` to ``speed`` (−1.0..+1.0). Out-of-range values are
        the peripheral wrapper's problem to validate; the HAL itself accepts
        whatever it's given."""

    @abc.abstractmethod
    async def get_speed(self, servo_id: str) -> float:
        """Return the last-set speed for ``servo_id``. Returns 0.0 if never set."""

    @abc.abstractmethod
    async def stop_all(self) -> None:
        """Set every known servo to speed 0. Called on shutdown for safety."""


# -----------------------------------------------------------------------------
# Addressable LED ring
# -----------------------------------------------------------------------------


class RingBackend(Backend):
    """NeoPixel-style addressable LED ring.

    Writes are *staged* — :meth:`set_pixel` and :meth:`fill` update an internal
    frame buffer; :meth:`show` flushes it to the strip. This matches both
    rpi_ws281x semantics and any sensible test/fake; it also means a peripheral
    can compose a full frame and commit atomically.
    """

    @property
    @abc.abstractmethod
    def pixel_count(self) -> int:
        """Number of pixels the backend will drive. Frame buffer is this long."""

    @abc.abstractmethod
    async def set_pixel(self, index: int, colour: Colour) -> None:
        """Stage ``colour`` for pixel ``index`` in [0, :attr:`pixel_count`)."""

    @abc.abstractmethod
    async def fill(self, colour: Colour) -> None:
        """Stage ``colour`` for every pixel."""

    @abc.abstractmethod
    async def show(self) -> None:
        """Flush the staged frame to the strip."""

    @abc.abstractmethod
    async def get_frame(self) -> tuple[Colour, ...]:
        """Return the currently *displayed* (last shown) frame. Length =
        :attr:`pixel_count`. Useful for tests and the ``status`` snapshot."""


# -----------------------------------------------------------------------------
# GPIO input + PWM output
# -----------------------------------------------------------------------------

GpioCallback = Callable[[GpioEdge], None]
"""Callback signature for edge events. Runs on the asyncio event loop thread."""


class GpioInputBackend(Backend):
    """Digital input pins with edge-callback delivery.

    Pull direction (up / down / none) is the backend's concern at construction
    time — the same logical "pressed = True" reading is exposed regardless.
    """

    @abc.abstractmethod
    async def read(self, pin: int) -> bool:
        """Return the current logical level on ``pin``."""

    @abc.abstractmethod
    async def on_edge(self, pin: int, callback: GpioCallback) -> None:
        """Register ``callback`` to fire on every edge of ``pin``.

        Multiple callbacks may register against the same pin; all fire in
        registration order on each edge.
        """


class GpioPwmBackend(Backend):
    """Digital PWM outputs (e.g. arcade button LED)."""

    @abc.abstractmethod
    async def set_duty(self, pin: int, duty: float) -> None:
        """Set PWM duty on ``pin`` to ``duty`` in [0.0, 1.0]."""

    @abc.abstractmethod
    async def get_duty(self, pin: int) -> float:
        """Return the last-set duty for ``pin``. Returns 0.0 if never set."""


# -----------------------------------------------------------------------------
# Serial (CYD UART link)
# -----------------------------------------------------------------------------


class SerialBackend(Backend):
    """Async newline-delimited serial link, UTF-8 encoded.

    The CYD UART grammar is line-oriented (e.g. ``mood:HAPPY\\n``), so the HAL
    works in whole lines rather than raw byte streams. Backends are responsible
    for framing.
    """

    @abc.abstractmethod
    async def write_line(self, line: str) -> None:
        """Encode ``line`` as UTF-8, append a newline, write. Awaits the write."""

    @abc.abstractmethod
    async def read_line(self) -> str:
        """Await the next newline-terminated line. Newline stripped from result.
        The trailing CR (CRLF endings) is also stripped if present."""


# -----------------------------------------------------------------------------
# Audio
# -----------------------------------------------------------------------------

AudioCallback = Callable[[bytes], None]
"""Callback for captured audio frames. Runs on the asyncio event loop thread."""


class AudioBackend(Backend):
    """Local audio I/O on whichever host runs the backend.

    The HAL is intentionally narrow — *one* local device, play whole payloads
    or stream them, capture into a callback. Multi-sink routing (local + LAN
    stream) is a peripheral concern that composes this backend with the
    network layer.
    """

    @abc.abstractmethod
    async def list_devices(self) -> dict:
        """Return currently enumerated audio devices.

        Shape::

            {
                "input":  [{"index": 0, "name": "...", "channels": 2, "samplerate": 44100}, ...],
                "output": [{"index": 0, "name": "...", "channels": 2, "samplerate": 48000}, ...],
                "default_input":  0,
                "default_output": 0,
            }

        Useful for clients that want to let users pick a device without
        needing shell access to the Pi.
        """

    @abc.abstractmethod
    async def play_wav(self, wav_bytes: bytes) -> None:
        """Play a WAV-encoded payload through the default output device.
        Awaits playback completion."""

    @abc.abstractmethod
    async def stream_pcm(
        self,
        pcm_chunks: "AsyncIterable[bytes]",
        *,
        samplerate: int,
        channels: int,
        sample_width: int = 2,
    ) -> None:
        """Stream raw PCM chunks to the output device with low latency.

        Playback begins as soon as the first chunk arrives; the backend does
        not buffer the whole stream. Awaits completion (until the async
        iterator is exhausted or cancelled).

        Args:
            pcm_chunks:   async iterator yielding raw PCM byte chunks.
            samplerate:   Hz (e.g. 48000).
            channels:     1 (mono) or 2 (stereo).
            sample_width: bytes per sample (default 2 for s16le).
        """

    @abc.abstractmethod
    async def start_capture(self, callback: AudioCallback) -> None:
        """Begin streaming captured audio frames to ``callback``.
        Subsequent calls before :meth:`stop_capture` replace the callback."""

    @abc.abstractmethod
    async def stop_capture(self) -> None:
        """Stop capture. Idempotent. Safe to call when no capture is running."""
