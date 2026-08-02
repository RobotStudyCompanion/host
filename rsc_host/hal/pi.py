"""Raspberry Pi HAL backends.

Concrete implementations of :mod:`rsc_host.hal.base` using:

  * **pigpio**          — servos (µs-precision pulse width, DMA-timed)
  * **gpiozero**        — button input, button LED PWM (pigpio pin factory)
  * **rpi_ws281x**      — NeoPixel ring (via ``neopixel`` on ``board.D12``)
  * **pyserial-asyncio** — CYD UART (``/dev/serial0``, 115200 8N1)
  * **sounddevice**     — audio playback and capture (ALSA)

Servo speed −1.0..+1.0 maps to per-servo pulse widths that match the tested
values from the RSC test script — including the right-flipper trim that
compensates for a specific servo pair's mismatch.

Callback threading: gpiozero fires button edges on a background thread.
:class:`PiGpioInputBackend` marshals them onto the asyncio loop via
``loop.call_soon_threadsafe``, honouring the HAL contract that edge callbacks
run on the loop thread.

Ring driver quirk: on some SK6812 RGBW chains, specifying the exact pixel
count under-drives the strip; the workaround is to allocate a longer buffer
(e.g. 24 for a 16-pixel ring). :class:`PiRingBackend` accepts a
``buffer_size`` parameter distinct from ``pixel_count`` for this.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
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
# Servo — pigpio
# -----------------------------------------------------------------------------

# Tested pulse-width values from the RSC test script.
# The right flipper needs a trim offset — some servos in this batch idle
# fast on the neutral pulse and need slightly asymmetric FWD/REV widths.
_SERVO_STOP_US = 1500

_SERVO_TIMINGS: dict[str, tuple[int, int]] = {
    # (reverse_us, forward_us)
    "left":  (1400, 1600),
    # Right flipper trimmed by 105 µs to match the left's speed.
    "right": (1650, 1350),
    "m3":    (1400, 1600),
}


class PiServoBackend(ServoBackend):
    """Servo control via pigpio's DMA-timed hardware pulse-width generator.

    Speed maps linearly across the per-servo (reverse_us, forward_us) range,
    passing through the neutral pulse at speed=0.
    """

    def __init__(
        self,
        pins: dict[str, int],
        pi_host: str = "localhost",
        pi_port: int = 8888,
    ) -> None:
        """
        Args:
            pins:    Mapping of servo_id → BCM GPIO pin.
            pi_host: pigpiod host (default localhost).
            pi_port: pigpiod port (default 8888).
        """
        self._pins = dict(pins)
        self._pi_host = pi_host
        self._pi_port = pi_port
        self._pi: "pigpio.pi | None" = None  # type: ignore[name-defined]
        self._speeds: dict[str, float] = {}

    async def start(self) -> None:
        import pigpio  # imported here so laptop dev doesn't need pigpio installed

        self._pi = pigpio.pi(self._pi_host, self._pi_port)
        if not self._pi.connected:
            self._pi = None
            raise RuntimeError(
                f"cannot connect to pigpiod at {self._pi_host}:{self._pi_port} — "
                "is the pigpiod service running? (sudo systemctl status pigpiod)"
            )
        # Explicitly park every servo at neutral on startup so a mid-motion
        # daemon crash doesn't leave a flipper spinning after restart.
        for pin in self._pins.values():
            self._pi.set_servo_pulsewidth(pin, _SERVO_STOP_US)
        log.info("PiServoBackend started (pins=%s)", self._pins)

    async def stop(self) -> None:
        if self._pi is None:
            return
        # 0 disables the servo signal entirely — cleaner than parking at neutral,
        # since some 360° servos still creep at 1500 µs.
        for pin in self._pins.values():
            try:
                self._pi.set_servo_pulsewidth(pin, 0)
            except Exception:
                log.exception("failed to zero servo on pin %d", pin)
        self._pi.stop()
        self._pi = None
        log.info("PiServoBackend stopped")

    async def set_speed(self, servo_id: str, speed: float) -> None:
        if self._pi is None:
            raise RuntimeError("PiServoBackend not started")
        pin = self._pins.get(servo_id)
        if pin is None:
            raise KeyError(f"unknown servo_id: {servo_id!r}")

        timing = _SERVO_TIMINGS.get(servo_id, (1400, 1600))
        reverse_us, forward_us = timing
        # Linear map −1..+1 across (reverse_us..forward_us) with STOP at 0.
        if speed >= 0:
            pw = int(_SERVO_STOP_US + (forward_us - _SERVO_STOP_US) * speed)
        else:
            pw = int(_SERVO_STOP_US + (_SERVO_STOP_US - reverse_us) * speed)
        self._pi.set_servo_pulsewidth(pin, pw)
        self._speeds[servo_id] = speed

    async def get_speed(self, servo_id: str) -> float:
        return self._speeds.get(servo_id, 0.0)

    async def stop_all(self) -> None:
        for servo_id in list(self._speeds.keys()):
            await self.set_speed(servo_id, 0.0)


# -----------------------------------------------------------------------------
# LED Ring — rpi_ws281x via neopixel
# -----------------------------------------------------------------------------


class PiRingBackend(RingBackend):
    """NeoPixel ring driven via the ``neopixel`` library (rpi_ws281x under it).

    The SK6812 RGBW chip in the RSC uses GRBW pixel order. Some driver
    versions under-clock a 16-pixel strip when the buffer is exactly 16 long;
    ``buffer_size`` overrides the allocated length (defaults to ``pixel_count``).
    """

    def __init__(
        self,
        pixel_count: int = 16,
        buffer_size: int | None = None,
        brightness: float = 0.3,
    ) -> None:
        self._pixel_count = pixel_count
        self._buffer_size = buffer_size or pixel_count
        self._brightness = brightness
        self._np: "neopixel.NeoPixel | None" = None  # type: ignore[name-defined]
        self._staged: list[Colour] = [Colour.black()] * pixel_count
        self._shown: list[Colour] = [Colour.black()] * pixel_count

    @property
    def pixel_count(self) -> int:
        return self._pixel_count

    async def start(self) -> None:
        import board
        import neopixel

        self._np = neopixel.NeoPixel(
            board.D12,                       # M1_PWM / J6 header on the HAT
            self._buffer_size,
            brightness=self._brightness,
            auto_write=False,
            pixel_order=neopixel.GRBW,       # SK6812 RGBW chain
        )
        self._np.fill((0, 0, 0, 0))
        self._np.show()
        log.info(
            "PiRingBackend started (pixels=%d, buffer=%d)",
            self._pixel_count, self._buffer_size,
        )

    async def stop(self) -> None:
        if self._np is None:
            return
        try:
            self._np.fill((0, 0, 0, 0))
            self._np.show()
        except Exception:
            log.exception("failed to clear ring on shutdown")
        self._np = None
        log.info("PiRingBackend stopped")

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
        if self._np is None:
            raise RuntimeError("PiRingBackend not started")
        # Push staged frame to the strip. W channel unused (RGB only).
        for i, colour in enumerate(self._staged):
            self._np[i] = (colour.r, colour.g, colour.b, 0)
        self._np.show()
        self._shown = list(self._staged)

    async def get_frame(self) -> tuple[Colour, ...]:
        return tuple(self._shown)


# -----------------------------------------------------------------------------
# GPIO input — gpiozero Button
# -----------------------------------------------------------------------------


class PiGpioInputBackend(GpioInputBackend):
    """Digital inputs via ``gpiozero.Button``.

    gpiozero's edge callbacks fire on a background thread. We capture the
    asyncio loop at start() and marshal each callback onto it via
    ``call_soon_threadsafe``, honouring the HAL contract that edge callbacks
    run on the loop thread.
    """

    def __init__(
        self,
        pins: Iterable[int],
        pull_up: bool = False,
        bounce_time: float = 0.02,
    ) -> None:
        """
        Args:
            pins:        BCM GPIO pins to configure as inputs.
            pull_up:     True for internal pull-up (button to GND);
                         False for pull-down (button to 3.3V — the arcade
                         button as wired on the HAT).
            bounce_time: gpiozero's hardware-adjacent debounce, in seconds.
        """
        self._pins = tuple(pins)
        self._pull_up = pull_up
        self._bounce_time = bounce_time
        self._buttons: dict[int, "gpiozero.Button"] = {}  # type: ignore[name-defined]
        self._callbacks: dict[int, list[GpioCallback]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        # Force gpiozero to use pigpio as its backend, matching the servo/PWM
        # side. Cleaner than mixing native + pigpio pin factories.
        import os
        os.environ.setdefault("GPIOZERO_PIN_FACTORY", "pigpio")
        import gpiozero

        self._loop = asyncio.get_running_loop()
        for pin in self._pins:
            b = gpiozero.Button(
                pin,
                pull_up=self._pull_up,
                bounce_time=self._bounce_time,
            )
            b.when_pressed = lambda p=pin: self._on_edge(p, Edge.RISING)
            b.when_released = lambda p=pin: self._on_edge(p, Edge.FALLING)
            self._buttons[pin] = b
        log.info("PiGpioInputBackend started (pins=%s)", self._pins)

    async def stop(self) -> None:
        for b in self._buttons.values():
            try:
                b.close()
            except Exception:
                log.exception("failed to close button")
        self._buttons.clear()
        self._callbacks.clear()
        self._loop = None
        log.info("PiGpioInputBackend stopped")

    async def read(self, pin: int) -> bool:
        b = self._buttons.get(pin)
        if b is None:
            raise KeyError(f"pin {pin} not configured as input")
        return bool(b.is_pressed)

    async def on_edge(self, pin: int, callback: GpioCallback) -> None:
        if pin not in self._buttons:
            raise KeyError(f"pin {pin} not configured as input")
        self._callbacks[pin].append(callback)

    def _on_edge(self, pin: int, edge: Edge) -> None:
        """Runs on gpiozero's background thread. Bounces the event onto the loop."""
        import time as _time
        event = GpioEdge(pin=pin, edge=edge, timestamp_ns=_time.monotonic_ns())
        loop = self._loop
        if loop is None:
            return
        for cb in self._callbacks.get(pin, ()):
            loop.call_soon_threadsafe(cb, event)


# -----------------------------------------------------------------------------
# GPIO PWM output — gpiozero PWMLED
# -----------------------------------------------------------------------------


class PiGpioPwmBackend(GpioPwmBackend):
    """PWM outputs via ``gpiozero.PWMLED``.

    On stop we additionally run ``pinctrl set <pin> op dl`` for each PWM pin.
    Without this, gpiozero occasionally leaves the line floating and the
    button LED stays dimly lit — behaviour verified in the RSC test script.
    """

    def __init__(self, pins: Iterable[int]) -> None:
        self._pins = tuple(pins)
        self._leds: dict[int, "gpiozero.PWMLED"] = {}  # type: ignore[name-defined]
        self._duties: dict[int, float] = {}

    async def start(self) -> None:
        import os
        os.environ.setdefault("GPIOZERO_PIN_FACTORY", "pigpio")
        import gpiozero

        for pin in self._pins:
            self._leds[pin] = gpiozero.PWMLED(pin)
        log.info("PiGpioPwmBackend started (pins=%s)", self._pins)

    async def stop(self) -> None:
        for pin, led in self._leds.items():
            try:
                led.off()
                led.close()
            except Exception:
                log.exception("failed to close PWMLED on pin %d", pin)
            # Belt-and-braces: force the line low via pinctrl.
            try:
                subprocess.run(
                    ["pinctrl", "set", str(pin), "op", "dl"],
                    check=False, capture_output=True,
                )
            except FileNotFoundError:
                # pinctrl not present (e.g. older Pi OS) — not fatal.
                pass
        self._leds.clear()
        log.info("PiGpioPwmBackend stopped")

    async def set_duty(self, pin: int, duty: float) -> None:
        led = self._leds.get(pin)
        if led is None:
            raise KeyError(f"pin {pin} not configured as PWM output")
        led.value = max(0.0, min(1.0, duty))
        self._duties[pin] = duty

    async def get_duty(self, pin: int) -> float:
        return self._duties.get(pin, 0.0)


# -----------------------------------------------------------------------------
# Serial — pyserial-asyncio for the CYD UART
# -----------------------------------------------------------------------------


class PiSerialBackend(SerialBackend):
    """Async newline-delimited UART for the CYD front-panel link.

    Uses pyserial-asyncio; the underlying device is typically
    ``/dev/serial0`` at 115200 8N1.
    """

    def __init__(
        self,
        device: str = "/dev/serial0",
        baudrate: int = 115200,
    ) -> None:
        self._device = device
        self._baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        import serial_asyncio  # pip install pyserial-asyncio

        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._device, baudrate=self._baudrate,
        )
        log.info(
            "PiSerialBackend started (device=%s, baud=%d)",
            self._device, self._baudrate,
        )

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                log.exception("failed to close serial writer")
        self._reader = None
        self._writer = None
        log.info("PiSerialBackend stopped")

    async def write_line(self, line: str) -> None:
        if self._writer is None:
            raise RuntimeError("PiSerialBackend not started")
        self._writer.write((line + "\n").encode("utf-8"))
        await self._writer.drain()

    async def read_line(self) -> str:
        if self._reader is None:
            raise RuntimeError("PiSerialBackend not started")
        raw = await self._reader.readline()
        line = raw.decode("utf-8", errors="replace")
        # Strip trailing CR (CRLF endings) and LF.
        return line.rstrip("\r\n")


# -----------------------------------------------------------------------------
# Audio — sounddevice (ALSA)
# -----------------------------------------------------------------------------


class PiAudioBackend(AudioBackend):
    """Local audio I/O via ``sounddevice``.

    Playback runs on a worker thread (sounddevice's blocking API) via
    :func:`asyncio.to_thread`. Capture uses sounddevice's callback API and
    marshals frames onto the asyncio loop, matching the HAL contract.
    """

    def __init__(
        self,
        input_device: int | str | None = None,
        output_device: int | str | None = None,
        samplerate: int = 16000,
        channels: int = 1,
    ) -> None:
        self._input_device = input_device
        self._output_device = output_device
        self._samplerate = samplerate
        self._channels = channels
        self._loop: asyncio.AbstractEventLoop | None = None
        self._capture_stream: "sounddevice.InputStream | None" = None  # type: ignore[name-defined]
        self._capture_callback: AudioCallback | None = None

    async def start(self) -> None:
        # Just capture the loop; sounddevice initialises lazily on stream open.
        self._loop = asyncio.get_running_loop()
        log.info(
            "PiAudioBackend started (in=%s, out=%s, sr=%d)",
            self._input_device, self._output_device, self._samplerate,
        )

    async def stop(self) -> None:
        await self.stop_capture()
        self._loop = None
        log.info("PiAudioBackend stopped")

    async def play_wav(self, wav_bytes: bytes) -> None:
        """Play a WAV payload. Blocks in a worker thread; awaits completion."""
        import io
        import soundfile as sf
        import sounddevice as sd

        def _play() -> None:
            data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            sd.play(data, samplerate=sr, device=self._output_device)
            sd.wait()

        await asyncio.to_thread(_play)

    async def start_capture(self, callback: AudioCallback) -> None:
        import sounddevice as sd

        if self._loop is None:
            raise RuntimeError("PiAudioBackend not started")

        # Replace any in-flight capture — matches the HAL contract.
        await self.stop_capture()
        self._capture_callback = callback

        loop = self._loop

        def _sd_callback(indata, frames, time_info, status) -> None:
            # Runs on sounddevice's audio thread; marshal to the loop.
            cb = self._capture_callback
            if cb is None:
                return
            payload = bytes(indata)
            loop.call_soon_threadsafe(cb, payload)

        self._capture_stream = sd.InputStream(
            device=self._input_device,
            samplerate=self._samplerate,
            channels=self._channels,
            dtype="int16",
            callback=_sd_callback,
        )
        self._capture_stream.start()

    async def stop_capture(self) -> None:
        if self._capture_stream is not None:
            try:
                self._capture_stream.stop()
                self._capture_stream.close()
            except Exception:
                log.exception("failed to close capture stream")
            self._capture_stream = None
        self._capture_callback = None
