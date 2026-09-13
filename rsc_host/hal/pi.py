"""Raspberry Pi HAL backends.

Concrete implementations of :mod:`rsc_host.hal.base` using:

  * **lgpio**            — servos (software-timed pulses, no daemon)
  * **gpiozero**         — button input and button LED PWM, on the *lgpio* pin
                           factory
  * **ring helper**      — NeoPixel ring, driven over a unix socket by the
                           privileged :mod:`rsc_host.ring_helper` process
  * **pyserial-asyncio** — CYD UART (``/dev/serial0``, 115200 8N1)
  * **ALSA**             — audio capture and playback through ``arecord`` and
                           ``aplay`` subprocesses

pigpio is gone
--------------
An earlier revision drove servos through ``pigpiod`` and forced gpiozero onto
the pigpio pin factory. That daemon's unit file carried a ``-t 0`` flag and an
``ExecStop`` that killed the transaction calling it, so every stop left DMA
channels and the PWM peripheral unrestored — which is what made the Pi look
incapable of driving several timed peripherals at once. It is not. With pigpio
masked and everything on lgpio, ring DMA, servo pulses, I2S audio, LED PWM and
button edges all run concurrently without contention.

**Do not reintroduce pigpio.** :func:`_ensure_gpiozero_factory` refuses to
start if gpiozero has resolved to the pigpio factory, so a stray
``GPIOZERO_PIN_FACTORY=pigpio`` in the environment fails loudly instead of
silently degrading.

Three lgpio traps worth knowing
-------------------------------
1. ``gpio_claim_output`` is **required** before ``tx_servo``. pigpio did not
   need it. Omitting it is a silent no-op — no error, no pulses, no motion.
2. ``tx_servo(chip, pin, 0)`` raises ``bad PWM micros`` if that pin has never
   had a wave started. :meth:`PiServoBackend._cease` guards for it.
3. Releasing a chardev line reverts the pin to input. On GPIO 24 that floats
   Q1's gate and lights the arcade LED, so :class:`PiGpioPwmBackend` also
   writes the pad register through ``pinctrl``. The durable fix is a
   pull-down resistor on the gate; nothing in software survives SIGKILL.

Servo idle discipline
---------------------
``tx_servo(chip, pin, 1500)`` leaves a continuous-rotation servo energised and
actively servoing. Slightly off true neutral it hunts — twitch, stop, drift,
twitch — which measures as +17 dB of hiss above 8 kHz and +4 to +5 dB inside
the speech band. That is the single largest interferer on the microphone.
:class:`PiServoBackend` therefore holds neutral briefly and then *ceases
pulses altogether* whenever a flipper is not moving.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import struct
import subprocess
import wave
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace

from rsc_host.errors import CalibrationError, PeripheralUnavailableError
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
from rsc_host.hal.dsp import CaptureChain, CaptureConfig, ReferenceTap, to_reference
from rsc_host.hal.types import Colour, Edge, GpioEdge
from rsc_host.ring_helper import (
    DEFAULT_SOCKET,
    OP_BRIGHTNESS,
    OP_CLEAR,
    OP_FRAME,
    OP_INFO,
    OP_PING,
    STATUS_OK,
)

log = logging.getLogger(__name__)


def _ensure_gpiozero_factory() -> str:
    """Pin gpiozero to lgpio and refuse to run on pigpio.

    Returns the resolved factory's class name. Raises
    :class:`PeripheralUnavailableError` if it resolved to pigpio, because that
    reintroduces exactly the contention this port exists to remove.
    """
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    from gpiozero import Device

    Device.ensure_pin_factory()
    name = type(Device.pin_factory).__name__
    if "pigpio" in name.lower():
        raise PeripheralUnavailableError(
            f"gpiozero resolved to {name}. pigpio must stay masked on this "
            "host — its unit file leaves DMA channels and the PWM peripheral "
            "unrestored on stop. Unset GPIOZERO_PIN_FACTORY or set it to "
            "'lgpio', and check: systemctl is-enabled pigpiod"
        )
    log.info("gpiozero pin factory: %s", name)
    return name


# -----------------------------------------------------------------------------
# Servos — lgpio
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServoCalibration:
    """Per-servo mapping from normalised speed to pulse width.

    Three quantities, deliberately kept separate because conflating them is
    how the old timing table went wrong:

    ``null_us``
        The pulse width at which this servo is genuinely stationary. Found by
        sweeping pulse width and locating the quiet plateau where the H-bridge
        stops hunting — ``rsc-test servo_null``. Left measured 1495 from a
        clean run; right is provisional at 1510 and wants one confirming run.

    ``span_us``
        Deflection from null that corresponds to full speed. This is where a
        *speed* mismatch between two physical servos is compensated: the right
        unit needs roughly 105 µs more deflection than the left to turn at the
        same rate. It is **not** a null offset.

    ``invert``
        True when the servo is mounted mirrored, so positive speed means the
        opposite pulse direction. The right flipper is mirrored on this
        chassis.
    """

    null_us: int = 1500
    span_us: int = 100
    invert: bool = False
    min_us: int = 900
    max_us: int = 2100

    def validate(self, servo_id: str = "servo") -> "ServoCalibration":
        if not self.min_us < self.null_us < self.max_us:
            raise CalibrationError(
                f"{servo_id}: null_us={self.null_us} outside "
                f"({self.min_us}, {self.max_us})"
            )
        if self.span_us <= 0:
            raise CalibrationError(
                f"{servo_id}: span_us must be > 0, got {self.span_us}"
            )
        if (
            self.null_us + self.span_us > self.max_us
            or self.null_us - self.span_us < self.min_us
        ):
            raise CalibrationError(
                f"{servo_id}: null_us={self.null_us} ± span_us={self.span_us} "
                f"leaves the {self.min_us}-{self.max_us} µs window"
            )
        return self

    def pulse_for(self, speed: float) -> int:
        """Normalised speed → pulse width in µs, clamped to the safe window."""
        s = -speed if self.invert else speed
        pw = int(round(self.null_us + self.span_us * s))
        return max(self.min_us, min(self.max_us, pw))

    def as_dict(self) -> dict:
        return {
            "null_us": self.null_us,
            "span_us": self.span_us,
            "invert": self.invert,
            "min_us": self.min_us,
            "max_us": self.max_us,
        }


#: Measured on the RSC chassis. Right is provisional — see the handover notes.
DEFAULT_CALIBRATION: dict[str, ServoCalibration] = {
    "left":  ServoCalibration(null_us=1495, span_us=100, invert=False),
    "right": ServoCalibration(null_us=1510, span_us=205, invert=True),
    "m3":    ServoCalibration(null_us=1500, span_us=100, invert=False),
}


class PiServoBackend(ServoBackend):
    """Continuous-rotation servos on lgpio, with pulses ceased when idle.

    Args:
        pins:        servo_id → BCM pin. Only pass servos that are actually
                     fitted; claiming a line the ring uses will fight it.
        calibration: servo_id → :class:`ServoCalibration`.
        chip:        gpiochip index (0 on a Pi 4).
        deadband:    speeds inside ±deadband count as stopped, so a ramp
                     through zero does not chatter the pulse train off and on.
        idle_ms:     how long to hold neutral before ceasing pulses. Gives the
                     servo time to settle rather than freezing mid-motion.
    """

    def __init__(
        self,
        pins: dict[str, int],
        calibration: dict[str, ServoCalibration] | None = None,
        *,
        chip: int = 0,
        deadband: float = 0.02,
        idle_ms: int = 120,
    ) -> None:
        self._pins = dict(pins)
        self._cal = dict(calibration or DEFAULT_CALIBRATION)
        self._chip_index = chip
        self._deadband = abs(deadband)
        self._idle_ms = max(0, idle_ms)
        self._lgpio = None
        self._chip: int | None = None
        self._claimed: list[int] = []
        self._speeds: dict[str, float] = {}
        self._live: set[int] = set()  # pins with a wave started
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}

    # ---- Lifecycle ----

    async def start(self) -> None:
        if self._chip is not None:
            return
        try:
            import lgpio
        except ImportError as exc:
            raise PeripheralUnavailableError(
                "lgpio is not installed. pip install lgpio"
            ) from exc
        self._lgpio = lgpio

        try:
            self._chip = lgpio.gpiochip_open(self._chip_index)
        except Exception as exc:
            raise PeripheralUnavailableError(
                f"cannot open gpiochip{self._chip_index}: {exc}. Is this user "
                "in the 'gpio' group? Check with: id -nG"
            ) from exc

        for servo_id, pin in self._pins.items():
            try:
                # Required by lgpio; pigpio did not need it. Omitting it makes
                # tx_servo a silent no-op — no error, no output, no motion.
                lgpio.gpio_claim_output(self._chip, pin, 0)
            except Exception as exc:
                await self._release()
                raise PeripheralUnavailableError(
                    f"cannot claim GPIO{pin} for servo {servo_id!r}: {exc}. "
                    "Another process may already hold the line."
                ) from exc
            self._claimed.append(pin)
            self._cal.setdefault(servo_id, ServoCalibration()).validate(servo_id)

        # Lines are claimed low and left unpulsed: servos stay inert until
        # commanded, so a restart mid-motion stops rather than resumes.
        log.info(
            "PiServoBackend started (pins=%s, deadband=%.3f, idle=%d ms)",
            self._pins, self._deadband, self._idle_ms,
        )

    async def stop(self) -> None:
        await self._cancel_all_idle()
        await self._release()
        log.info("PiServoBackend stopped")

    async def _release(self) -> None:
        if self._chip is None or self._lgpio is None:
            self._chip = None
            return
        for pin in list(self._claimed):
            self._cease(pin)
        await asyncio.sleep(0.05)  # let the final pulse finish before freeing
        for pin in list(self._claimed):
            try:
                self._lgpio.gpio_free(self._chip, pin)
            except Exception:
                log.exception("failed to free GPIO%d", pin)
        self._claimed.clear()
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            log.exception("failed to close gpiochip")
        self._chip = None

    # ---- Pulse plumbing ----

    def _tx(self, pin: int, micros: int) -> None:
        assert self._lgpio is not None and self._chip is not None
        self._lgpio.tx_servo(self._chip, pin, micros)
        self._live.add(pin)

    def _cease(self, pin: int) -> None:
        """Stop pulsing ``pin``. Safe on a pin that never had a wave."""
        if self._lgpio is None or self._chip is None:
            return
        try:
            self._lgpio.tx_servo(self._chip, pin, 0)
        except Exception:
            # 'bad PWM micros' when no wave exists on this pin — expected.
            pass
        self._live.discard(pin)

    def _cancel_idle(self, servo_id: str) -> None:
        task = self._idle_tasks.pop(servo_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_all_idle(self) -> None:
        for servo_id in list(self._idle_tasks):
            self._cancel_idle(servo_id)
        await asyncio.sleep(0)

    async def _idle_after(self, servo_id: str, pin: int) -> None:
        try:
            await asyncio.sleep(self._idle_ms / 1000.0)
        except asyncio.CancelledError:
            return
        self._cease(pin)
        log.debug("servo %s: pulses ceased (idle)", servo_id)

    # ---- Calibration ----

    def calibration(self, servo_id: str | None = None) -> dict:
        """Current calibration, for the ``servo.calibration`` verb."""
        if servo_id is not None:
            cal = self._cal.get(servo_id)
            if cal is None:
                raise KeyError(f"unknown servo_id: {servo_id!r}")
            return {servo_id: cal.as_dict()}
        return {k: v.as_dict() for k, v in self._cal.items()}

    def set_calibration(self, servo_id: str, **changes) -> dict:
        """Update calibration in memory and return the new values.

        Changes apply from the next :meth:`set_speed`. They are *not*
        persisted — write the values into the unit file's environment once a
        confirming ``servo_null`` run agrees with them.
        """
        if servo_id not in self._pins:
            raise KeyError(f"unknown servo_id: {servo_id!r}")
        current = self._cal.get(servo_id, ServoCalibration())
        unknown = set(changes) - set(ServoCalibration.__dataclass_fields__)
        if unknown:
            raise CalibrationError(f"unknown calibration fields: {sorted(unknown)}")
        updated = replace(current, **changes).validate(servo_id)
        self._cal[servo_id] = updated
        log.info("servo %s recalibrated: %s", servo_id, updated.as_dict())
        return updated.as_dict()

    # ---- ServoBackend ----

    async def set_speed(self, servo_id: str, speed: float) -> None:
        if self._chip is None:
            raise PeripheralUnavailableError("PiServoBackend not started")
        pin = self._pins.get(servo_id)
        if pin is None:
            raise KeyError(f"unknown servo_id: {servo_id!r}")

        self._cancel_idle(servo_id)

        if abs(speed) < self._deadband:
            self._speeds[servo_id] = 0.0
            if pin not in self._live:
                return  # already silent; nothing to wind down
            cal = self._cal.get(servo_id, ServoCalibration())
            self._tx(pin, cal.null_us)
            if self._idle_ms == 0:
                self._cease(pin)
            else:
                self._idle_tasks[servo_id] = asyncio.create_task(
                    self._idle_after(servo_id, pin), name=f"servo-{servo_id}-idle"
                )
            return

        cal = self._cal.get(servo_id, ServoCalibration())
        self._tx(pin, cal.pulse_for(speed))
        self._speeds[servo_id] = speed

    async def get_speed(self, servo_id: str) -> float:
        return self._speeds.get(servo_id, 0.0)

    async def stop_all(self) -> None:
        """Immediate stop: neutral, then cease pulses without the idle linger."""
        await self._cancel_all_idle()
        for servo_id, pin in self._pins.items():
            self._speeds[servo_id] = 0.0
            if pin in self._live:
                cal = self._cal.get(servo_id, ServoCalibration())
                self._tx(pin, cal.null_us)
        if self._live:
            await asyncio.sleep(0.1)
        for pin in list(self._live):
            self._cease(pin)


# -----------------------------------------------------------------------------
# LED ring — privileged helper client (or direct, when running as root)
# -----------------------------------------------------------------------------

_RING_HEADER = struct.Struct(">BH")


class RingHelperClient:
    """Client for :mod:`rsc_host.ring_helper` over its unix socket.

    Reconnects once on a broken connection, so restarting the helper does not
    require restarting the daemon.
    """

    def __init__(self, path: str, timeout: float = 2.0) -> None:
        self.path = path
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.path), timeout=self.timeout
        )

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def request(self, op: int, payload: bytes = b"") -> bytes:
        async with self._lock:
            for attempt in (0, 1):
                if self._writer is None:
                    try:
                        await self.connect()
                    except Exception as exc:
                        raise PeripheralUnavailableError(
                            f"ring helper not reachable at {self.path}: {exc}. "
                            "Is rsc-ring.service running?"
                        ) from exc
                try:
                    return await self._exchange(op, payload)
                except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
                    await self.close()
                    if attempt == 1:
                        raise PeripheralUnavailableError(
                            f"ring helper connection lost: {exc}"
                        ) from exc
            raise PeripheralUnavailableError("ring helper unreachable")

    async def _exchange(self, op: int, payload: bytes) -> bytes:
        assert self._writer is not None and self._reader is not None
        self._writer.write(_RING_HEADER.pack(op, len(payload)) + payload)
        await self._writer.drain()
        header = await asyncio.wait_for(
            self._reader.readexactly(_RING_HEADER.size), timeout=self.timeout
        )
        status, length = _RING_HEADER.unpack(header)
        body = await self._reader.readexactly(length) if length else b""
        if status != STATUS_OK:
            raise PeripheralUnavailableError(
                f"ring helper rejected op 0x{op:02x}: "
                f"{body.decode('utf-8', 'replace')}"
            )
        return body


class PiRingBackend(RingBackend):
    """NeoPixel ring, driven through the privileged helper by default.

    The ring is the one peripheral on this chassis that cannot run
    unprivileged: ``rpi_ws281x`` mmaps ``/dev/mem`` to reach PWM0 and DMA, and
    that needs ``CAP_SYS_RAWIO``. GPIO 12 is fixed by the wiring, so SPI (and
    the ``spi`` group) is not available.

    Modes:

    ``auto``    root → ``direct``; otherwise ``helper`` if the socket answers;
                otherwise unavailable, and the rest of the daemon carries on.
    ``helper``  always talk to :mod:`rsc_host.ring_helper`.
    ``direct``  drive the strip in-process. Only works as root — useful on the
                bench, not in the unit.
    ``off``     never touch the ring. Ring verbs return PERIPHERAL_UNAVAILABLE.

    Colour handling: :class:`~rsc_host.hal.types.Colour` is RGB, but the strip
    is SKC6812 **RGBW**. With ``white_mode='extract'`` the common component is
    moved into the dedicated white LED, which is both brighter and a cleaner
    white than mixing it from RGB.
    """

    def __init__(
        self,
        pixel_count: int = 16,
        *,
        mode: str = "auto",
        socket_path: str = DEFAULT_SOCKET,
        brightness: float = 0.3,
        gpio: int = 12,
        white_mode: str = "extract",
    ) -> None:
        if mode not in ("auto", "helper", "direct", "off"):
            raise ValueError(f"unknown ring mode: {mode!r}")
        if white_mode not in ("extract", "off"):
            raise ValueError(f"unknown white_mode: {white_mode!r}")
        self._pixel_count = pixel_count
        self._mode = mode
        self._socket_path = socket_path
        self._brightness = brightness
        self._gpio = gpio
        self._white_mode = white_mode
        self._client: RingHelperClient | None = None
        self._driver = None  # ring_helper.RingDriver in direct mode
        self._resolved = "off"
        self._staged: list[Colour] = [Colour.black()] * pixel_count
        self._shown: list[Colour] = [Colour.black()] * pixel_count
        self._reason: str | None = None

    @property
    def pixel_count(self) -> int:
        return self._pixel_count

    @property
    def available(self) -> bool:
        return self._resolved in ("helper", "direct")

    def status(self) -> dict:
        return {
            "available": self.available,
            "mode": self._resolved,
            "requested_mode": self._mode,
            "socket": self._socket_path,
            "pixels": self._pixel_count,
            "brightness": self._brightness,
            "white_mode": self._white_mode,
            "reason": self._reason,
        }

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Resolve a driving mode. Never raises — an unavailable ring must not
        stop the daemon booting."""
        if self._mode == "off":
            self._resolved = "off"
            self._reason = "disabled by configuration"
            log.info("PiRingBackend disabled (mode=off)")
            return

        want_direct = self._mode == "direct" or (
            self._mode == "auto" and os.geteuid() == 0
        )
        if want_direct and await self._start_direct():
            return
        if self._mode == "direct":
            return  # _start_direct already recorded the reason
        if await self._start_helper():
            return

        self._resolved = "off"
        if self._reason is None:
            self._reason = "no privileged path to the ring"
        log.warning(
            "ring unavailable (%s). Everything else runs normally; ring verbs "
            "will return PERIPHERAL_UNAVAILABLE. Start the helper with: "
            "sudo systemctl start rsc-ring.service",
            self._reason,
        )

    async def _start_direct(self) -> bool:
        from rsc_host.ring_helper import RingDriver

        try:
            driver = RingDriver(
                pin=self._gpio, pixels=self._pixel_count, brightness=self._brightness
            )
            await asyncio.to_thread(driver.start)
        except Exception as exc:
            self._reason = f"direct mode failed: {exc}"
            log.warning("ring direct mode unavailable: %s", exc)
            return False
        self._driver = driver
        self._resolved = "direct"
        self._reason = None
        log.info("PiRingBackend started in direct mode (running as root)")
        return True

    async def _start_helper(self) -> bool:
        client = RingHelperClient(self._socket_path)
        try:
            await client.request(OP_PING)
        except Exception as exc:
            self._reason = f"helper not reachable: {exc}"
            await client.close()
            return False
        self._client = client
        self._resolved = "helper"
        self._reason = None
        try:
            await client.request(
                OP_BRIGHTNESS,
                bytes([int(max(0.0, min(1.0, self._brightness)) * 255)]),
            )
            info = await client.request(OP_INFO)
            log.info(
                "PiRingBackend using helper at %s: %s",
                self._socket_path, info.decode("utf-8", "replace"),
            )
        except Exception:
            log.exception("ring helper handshake incomplete; continuing")
        return True

    async def stop(self) -> None:
        try:
            if self._resolved == "helper" and self._client is not None:
                await self._client.request(OP_CLEAR)
            elif self._resolved == "direct" and self._driver is not None:
                await asyncio.to_thread(self._driver.clear)
        except Exception:
            log.exception("failed to clear ring on shutdown")
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._driver is not None:
            try:
                await asyncio.to_thread(self._driver.stop)
            except Exception:
                log.exception("failed to release ring driver")
            self._driver = None
        self._resolved = "off"
        log.info("PiRingBackend stopped")

    # ---- RingBackend ----

    async def set_pixel(self, index: int, colour: Colour) -> None:
        if not 0 <= index < self._pixel_count:
            raise IndexError(
                f"pixel index {index} out of range [0, {self._pixel_count})"
            )
        self._staged[index] = colour

    async def fill(self, colour: Colour) -> None:
        self._staged = [colour] * self._pixel_count

    async def show(self) -> None:
        if not self.available:
            raise PeripheralUnavailableError(f"ring unavailable: {self._reason}")
        payload = self._pack(self._staged)
        if self._resolved == "helper":
            assert self._client is not None
            await self._client.request(OP_FRAME, payload)
        else:
            assert self._driver is not None
            await asyncio.to_thread(self._driver.frame, payload)
        self._shown = list(self._staged)

    async def get_frame(self) -> tuple[Colour, ...]:
        return tuple(self._shown)

    def _pack(self, frame: list[Colour]) -> bytes:
        out = bytearray(len(frame) * 4)
        extract = self._white_mode == "extract"
        for i, c in enumerate(frame):
            if extract:
                w = min(c.r, c.g, c.b)
                out[i * 4 : i * 4 + 4] = bytes((c.r - w, c.g - w, c.b - w, w))
            else:
                out[i * 4 : i * 4 + 4] = bytes((c.r, c.g, c.b, 0))
        return bytes(out)


# -----------------------------------------------------------------------------
# GPIO input — gpiozero Button on the lgpio factory
# -----------------------------------------------------------------------------


class PiGpioInputBackend(GpioInputBackend):
    """Digital inputs via ``gpiozero.Button``.

    gpiozero handles the pull direction correctly by itself:
    ``Button(pin, pull_up=False)`` claims the line with a pull-down. Reading
    the line raw with ``lgpio.gpio_claim_input`` and no pull leaves it
    floating — the first press drives it high and pin capacitance holds it
    there indefinitely, so the line reads 1 forever.

    **Only one holder per line.** A gpiozero ``Button`` and a raw
    ``gpio_claim_input`` on the same pin yields ``GPIO busy``. This backend is
    the sole holder of GPIO 23.

    gpiozero fires edges on a background thread; each one is marshalled onto
    the asyncio loop, honouring the HAL contract.
    """

    def __init__(
        self,
        pins: Iterable[int],
        pull_up: bool = False,
        bounce_time: float = 0.02,
    ) -> None:
        self._pins = tuple(pins)
        self._pull_up = pull_up
        self._bounce_time = bounce_time
        self._buttons: dict[int, object] = {}
        self._callbacks: dict[int, list[GpioCallback]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        _ensure_gpiozero_factory()
        import gpiozero

        self._loop = asyncio.get_running_loop()
        for pin in self._pins:
            try:
                b = gpiozero.Button(
                    pin, pull_up=self._pull_up, bounce_time=self._bounce_time
                )
            except Exception as exc:
                raise PeripheralUnavailableError(
                    f"cannot claim GPIO{pin} as input: {exc}. Another holder "
                    "on the same line gives 'GPIO busy'."
                ) from exc
            b.when_pressed = lambda p=pin: self._on_edge(p, Edge.RISING)
            b.when_released = lambda p=pin: self._on_edge(p, Edge.FALLING)
            self._buttons[pin] = b
        log.info("PiGpioInputBackend started (pins=%s)", self._pins)

    async def stop(self) -> None:
        for b in self._buttons.values():
            try:
                b.close()  # type: ignore[attr-defined]
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
        return bool(b.is_pressed)  # type: ignore[attr-defined]

    async def on_edge(self, pin: int, callback: GpioCallback) -> None:
        if pin not in self._buttons:
            raise KeyError(f"pin {pin} not configured as input")
        self._callbacks[pin].append(callback)

    def _on_edge(self, pin: int, edge: Edge) -> None:
        """Runs on gpiozero's background thread. Bounces onto the loop."""
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

    On stop we also run ``pinctrl set <pin> op dl``. Releasing a chardev line
    reverts the pin to input, which floats Q1's gate and lights the arcade LED.
    Only pinctrl sticks, because it writes the pad registers directly.

    Nothing here survives SIGKILL. The durable fix is a pull-down resistor on
    Q1's gate — worth raising with whoever owns the board.
    """

    def __init__(self, pins: Iterable[int]) -> None:
        self._pins = tuple(pins)
        self._leds: dict[int, object] = {}
        self._duties: dict[int, float] = {}

    async def start(self) -> None:
        _ensure_gpiozero_factory()
        import gpiozero

        for pin in self._pins:
            try:
                self._leds[pin] = gpiozero.PWMLED(pin)
            except Exception as exc:
                raise PeripheralUnavailableError(
                    f"cannot claim GPIO{pin} for PWM: {exc}"
                ) from exc
        log.info("PiGpioPwmBackend started (pins=%s)", self._pins)

    async def stop(self) -> None:
        for pin, led in self._leds.items():
            try:
                led.off()  # type: ignore[attr-defined]
                led.close()  # type: ignore[attr-defined]
            except Exception:
                log.exception("failed to close PWMLED on pin %d", pin)
            self.force_low(pin)
        self._leds.clear()
        log.info("PiGpioPwmBackend stopped")

    @staticmethod
    def force_low(pin: int) -> None:
        """Drive ``pin`` low at the pad register and leave it there."""
        try:
            subprocess.run(
                ["pinctrl", "set", str(pin), "op", "dl"],
                check=False, capture_output=True,
            )
        except FileNotFoundError:
            log.debug("pinctrl absent; cannot pin GPIO%d low after release", pin)

    async def set_duty(self, pin: int, duty: float) -> None:
        led = self._leds.get(pin)
        if led is None:
            raise KeyError(f"pin {pin} not configured as PWM output")
        led.value = max(0.0, min(1.0, duty))  # type: ignore[attr-defined]
        self._duties[pin] = duty

    async def get_duty(self, pin: int) -> float:
        return self._duties.get(pin, 0.0)


# -----------------------------------------------------------------------------
# Serial — pyserial-asyncio for the CYD UART
# -----------------------------------------------------------------------------


class PiSerialBackend(SerialBackend):
    """Async newline-delimited UART for the CYD front-panel link.

    Fails soft. A missing or unopenable ``/dev/serial0`` marks the link
    unavailable rather than aborting startup: the CYD is a display, and the
    robot is still useful without it. Writes then raise
    :class:`PeripheralUnavailableError`, and reads park forever so the CYD
    bridge's reader loop waits quietly instead of spinning on failures.
    """

    def __init__(
        self,
        device: str = "/dev/serial0",
        baudrate: int = 115200,
        *,
        required: bool = False,
    ) -> None:
        self._device = device
        self._baudrate = baudrate
        self._required = required
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._never = asyncio.Event()  # never set; parks read_line()

    @property
    def available(self) -> bool:
        return self._reader is not None

    async def start(self) -> None:
        try:
            import serial_asyncio  # noqa: PLC0415 — optional dependency

            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._device, baudrate=self._baudrate
            )
        except Exception as exc:
            if self._required:
                raise PeripheralUnavailableError(
                    f"cannot open CYD serial {self._device}: {exc}"
                ) from exc
            log.warning(
                "CYD serial unavailable (%s: %s); front panel disabled, "
                "everything else runs normally", self._device, exc,
            )
            self._reader = self._writer = None
            return
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
            raise PeripheralUnavailableError(f"CYD serial {self._device} is not open")
        self._writer.write((line + "\n").encode("utf-8"))
        await self._writer.drain()

    async def read_line(self) -> str:
        if self._reader is None:
            await self._never.wait()  # park until cancelled at shutdown
            return ""
        raw = await self._reader.readline()
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")


# -----------------------------------------------------------------------------
# Audio — ALSA via arecord / aplay
# -----------------------------------------------------------------------------

#: The known-good WM8960 mixer state. Gain goes in the analogue boost *ahead*
#: of the ADC, not the digital Capture control behind it: boost improves the
#: signal relative to the noise floor, Capture amplifies both equally. ALC at
#: max gain pumps and warbles on a quiet room, so it stays off.
MIXER_PRESET: tuple[tuple[str, str], ...] = (
    ("ALC Function", "Off"),
    ("ADC High Pass Filter", "on"),
    ("Left Boost Mixer LINPUT1", "on"),
    ("Right Boost Mixer RINPUT1", "on"),
    ("Left Input Boost Mixer LINPUT1", "3"),    # +29 dB analogue
    ("Right Input Boost Mixer RINPUT1", "3"),
    ("Capture", "35"),                          # +9 dB digital
    ("Playback", "255"),                        # 0 dB, DAC full scale
    ("Speaker", "121"),                         # 0 dB
    ("Headphone", "121"),
    ("Left Output Mixer PCM", "on"),            # without these, near-silence
    ("Right Output Mixer PCM", "on"),
    ("DAC Mono Mix", "Mono"),                   # the JST speaker is mono
)

#: Controls worth reporting in a status snapshot.
MIXER_REPORT: tuple[str, ...] = (
    "ALC Function",
    "ADC High Pass Filter",
    "Left Boost Mixer LINPUT1",
    "Left Input Boost Mixer LINPUT1",
    "Capture",
    "Playback",
    "Speaker",
    "Headphone",
    "Left Output Mixer PCM",
    "DAC Mono Mix",
)


class PiAudioBackend(AudioBackend):
    """Capture and playback through ALSA command-line tools.

    Why subprocesses rather than PortAudio: the verified capture recipe on this
    HAT is ``plughw:0,0`` at 48 kHz, and PortAudio cannot address a ``plug``
    device — it opens ``hw:`` and negotiates its own rate. Asking this driver
    for 16 kHz produces audible warble, which is exactly the failure that route
    invites. ``arecord``/``aplay`` take the device string verbatim, so what
    runs in production is what was measured on the bench.

    It also gives full duplex for nothing: capture and playback are separate
    processes against the same codec, whose ADC and DAC paths are independent.

    Capture always runs at the device's native rate; rate conversion happens in
    :class:`~rsc_host.hal.dsp.CaptureChain`, never in ALSA.
    """

    def __init__(
        self,
        input_device: str = "plughw:0,0",
        output_device: str = "plughw:0,0",
        capture: CaptureConfig | None = None,
        *,
        mixer_card: int | str = 0,
        apply_mixer_preset: bool = True,
        arecord_bin: str = "arecord",
        aplay_bin: str = "aplay",
        amixer_bin: str = "amixer",
    ) -> None:
        self._input_device = str(input_device)
        self._output_device = str(output_device)
        self._config = capture or CaptureConfig()
        self._mixer_card = str(mixer_card)
        self._apply_preset_on_start = apply_mixer_preset
        self._arecord = arecord_bin
        self._aplay = aplay_bin
        self._amixer_bin = amixer_bin

        self._loop: asyncio.AbstractEventLoop | None = None
        self._chain: CaptureChain | None = None
        self._reference: ReferenceTap | None = None
        self._reference_warned = False
        self._capture_proc: asyncio.subprocess.Process | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._capture_callback: AudioCallback | None = None
        self._play_procs: set[asyncio.subprocess.Process] = set()

    # ---- Lifecycle ----

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for binary in (self._arecord, self._aplay):
            if shutil.which(binary) is None:
                raise PeripheralUnavailableError(
                    f"{binary} not found. Install alsa-utils: "
                    "sudo apt install alsa-utils"
                )
        self._reference = ReferenceTap(self._config.stream_rate)
        if self._apply_preset_on_start:
            try:
                applied = await self.mixer_apply_preset()
                log.info(
                    "WM8960 mixer preset applied (%d/%d controls)",
                    sum(1 for v in applied.values() if v == "ok"), len(applied),
                )
            except Exception:
                log.exception("mixer preset failed; continuing with current state")
        log.info(
            "PiAudioBackend started (in=%s, out=%s, %d Hz %d ch -> %d Hz mono)",
            self._input_device, self._output_device,
            self._config.device_rate, self._config.device_channels,
            self._config.stream_rate,
        )

    async def stop(self) -> None:
        await self.stop_capture()
        for proc in list(self._play_procs):
            await self._kill(proc)
        self._play_procs.clear()
        self._loop = None
        log.info("PiAudioBackend stopped")

    # ---- Capture ----

    async def start_capture(self, callback: AudioCallback) -> None:
        if self._loop is None:
            raise PeripheralUnavailableError("PiAudioBackend not started")
        await self.stop_capture()

        cfg = self._config
        self._chain = CaptureChain(cfg, self._reference)
        self._capture_callback = callback

        args = [
            self._arecord,
            "-D", self._input_device,
            "-f", "S16_LE",
            "-r", str(cfg.device_rate),
            "-c", str(cfg.device_channels),
            "-t", "raw",
            f"--period-size={cfg.frame_samples}",
            f"--buffer-size={cfg.frame_samples * 4}",
            "-q",
            "-",
        ]
        log.info("capture: %s", " ".join(args))
        try:
            self._capture_proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            self._chain = None
            self._capture_callback = None
            raise PeripheralUnavailableError(
                f"cannot start capture on {self._input_device}: {exc}"
            ) from exc

        self._capture_task = asyncio.create_task(
            self._capture_loop(), name="alsa-capture"
        )

    async def _capture_loop(self) -> None:
        """Read device periods, run the chain, hand frames to the callback.

        Runs on the event loop, so the callback fires on the loop thread
        exactly as the HAL contract requires — no threadsafe marshalling
        needed, unlike the PortAudio path this replaced.
        """
        proc = self._capture_proc
        chain = self._chain
        callback = self._capture_callback
        if proc is None or chain is None or callback is None or proc.stdout is None:
            return
        frame_bytes = self._config.frame_bytes
        try:
            while True:
                try:
                    raw = await proc.stdout.readexactly(frame_bytes)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        log.debug("capture: dropped %d trailing bytes", len(exc.partial))
                    break
                try:
                    frame = chain.process(raw)
                except Exception:
                    log.exception("capture chain raised; ending session")
                    break
                if frame:
                    callback(frame)
        except asyncio.CancelledError:
            raise
        finally:
            if proc.returncode not in (None, 0):
                stderr = b""
                if proc.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(proc.stderr.read(), 0.5)
                    except Exception:
                        pass
                log.error(
                    "arecord exited %s: %s",
                    proc.returncode, stderr.decode("utf-8", "replace").strip(),
                )
            # Sentinel so the /audio/in handler closes the socket cleanly
            # rather than hanging on a stream that has already ended.
            cb = self._capture_callback
            if cb is not None:
                try:
                    cb(b"")
                except Exception:
                    log.exception("capture end-of-stream callback raised")

    async def stop_capture(self) -> None:
        task, self._capture_task = self._capture_task, None
        proc, self._capture_proc = self._capture_proc, None
        self._capture_callback = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if proc is not None:
            await self._kill(proc)
        if self._chain is not None:
            self._chain.close()
            self._chain = None

    # ---- Capture tuning (optional capability) ----

    def capture_config(self) -> dict:
        cfg = self._chain.config if self._chain is not None else self._config
        out = cfg.as_dict()
        out["capturing"] = self._chain is not None
        out["input_device"] = self._input_device
        out["output_device"] = self._output_device
        out["valid_stream_rates"] = list(
            CaptureConfig.valid_stream_rates(cfg.device_rate)
        )
        return out

    def retune_capture(self, **changes) -> dict:
        """Apply capture parameters live, or stage them for the next session.

        Raises ValueError on an invalid combination without disturbing the
        running chain.
        """
        if self._chain is not None:
            self._config = self._chain.retune(**changes)
        else:
            self._config = replace(self._config, **changes)  # validates
            log.info("capture config staged for next session: %s", changes)
        return self.capture_config()

    def capture_stats(self) -> dict:
        if self._chain is None:
            return {"capturing": False, "config": self._config.as_dict()}
        stats = self._chain.stats()
        stats["capturing"] = True
        return stats

    def reset_capture_stats(self) -> None:
        if self._chain is not None:
            self._chain.reset_stats()

    # ---- Playback ----

    async def play_wav(self, wav_bytes: bytes) -> None:
        args = [self._aplay, "-q", "-D", self._output_device, "-t", "wav", "-"]
        self._tap_wav(wav_bytes)
        await self._run_sink(args, self._chunks(wav_bytes))

    async def stream_pcm(
        self,
        pcm_chunks,
        *,
        samplerate: int,
        channels: int,
        sample_width: int = 2,
    ) -> None:
        if sample_width != 2:
            raise NotImplementedError(
                f"sample_width={sample_width} not supported; only s16le (2)"
            )
        args = [
            self._aplay, "-q",
            "-D", self._output_device,
            "-f", "S16_LE",
            "-r", str(samplerate),
            "-c", str(channels),
            "-t", "raw",
            "-",
        ]

        async def _tapped():
            async for chunk in pcm_chunks:
                self._tap_pcm(chunk, samplerate=samplerate, channels=channels)
                yield chunk

        await self._run_sink(args, _tapped())

    @staticmethod
    async def _chunks(data: bytes, size: int = 32768):
        for i in range(0, len(data), size):
            yield data[i : i + size]

    async def _run_sink(self, args: list[str], chunks) -> None:
        """Feed an aplay process, killing it if we are cancelled.

        Cancellation matters: ``audio.stop_play`` must actually silence the
        speaker, not merely stop feeding a process that still has a second of
        buffered audio to get through.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._play_procs.add(proc)
        try:
            assert proc.stdin is not None
            async for chunk in chunks:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            await proc.wait()
            if proc.returncode not in (0, None):
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                raise RuntimeError(
                    f"aplay exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', 'replace').strip()}"
                )
        except asyncio.CancelledError:
            await self._kill(proc)
            raise
        except (BrokenPipeError, ConnectionResetError):
            await self._kill(proc)
        finally:
            self._play_procs.discard(proc)

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    # ---- AEC reference tap ----

    def _tap_pcm(self, chunk: bytes, *, samplerate: int, channels: int) -> None:
        tap = self._reference
        if tap is None:
            return
        mono = to_reference(
            chunk, samplerate=samplerate, channels=channels, target_rate=tap.rate
        )
        if mono is None:
            if not self._reference_warned:
                log.warning(
                    "playback at %d Hz does not divide the AEC rate %d Hz; the "
                    "echo reference will be empty for this stream",
                    samplerate, tap.rate,
                )
                self._reference_warned = True
            return
        tap.push(mono)

    def _tap_wav(self, wav_bytes: bytes) -> None:
        tap = self._reference
        if tap is None:
            return
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as w:
                if w.getsampwidth() != 2:
                    return
                rate, channels = w.getframerate(), w.getnchannels()
                pcm = w.readframes(w.getnframes())
        except Exception:
            log.debug("could not parse WAV for the echo reference", exc_info=True)
            return
        self._tap_pcm(pcm, samplerate=rate, channels=channels)

    # ---- Devices and mixer ----

    async def list_devices(self) -> dict:
        """Enumerate ALSA cards and PCM names.

        Returns both the card list (``arecord -l``) and the PCM device strings
        (``arecord -L``), because what actually goes in
        ``RSC_HOST_AUDIO_INPUT`` is a PCM name such as ``plughw:0,0``, not a
        card index.
        """

        async def _run(args: list[str]) -> str:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await proc.communicate()
                return out.decode("utf-8", "replace")
            except Exception:
                log.exception("failed to run %s", args[0])
                return ""

        def _cards(text: str) -> list[dict]:
            cards = []
            for line in text.splitlines():
                if line.startswith("card "):
                    head, _, tail = line.partition(":")
                    try:
                        index = int(head.split()[1].split(",")[0])
                    except (IndexError, ValueError):
                        continue
                    cards.append({"index": index, "name": tail.strip()})
            return cards

        def _pcms(text: str) -> list[str]:
            return [
                line.strip()
                for line in text.splitlines()
                if line and not line[0].isspace()
            ]

        rec_l, play_l, rec_big_l, play_big_l = await asyncio.gather(
            _run([self._arecord, "-l"]),
            _run([self._aplay, "-l"]),
            _run([self._arecord, "-L"]),
            _run([self._aplay, "-L"]),
        )
        return {
            "input": _cards(rec_l),
            "output": _cards(play_l),
            "input_pcms": _pcms(rec_big_l),
            "output_pcms": _pcms(play_big_l),
            "default_input": self._input_device,
            "default_output": self._output_device,
        }

    async def _amixer(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self._amixer_bin, "-c", self._mixer_card, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace")

    @staticmethod
    def _parse_amixer(text: str) -> str | None:
        for line in text.splitlines():
            head, _, tail = line.strip().partition(":")
            if head in ("Mono", "Front Left", "Item0") and tail.strip():
                return tail.strip()
        return None

    async def mixer_get(self, names: Iterable[str] | None = None) -> dict:
        """Read mixer controls. Defaults to the ones that matter here."""
        wanted = tuple(names) if names is not None else MIXER_REPORT
        out: dict[str, str | None] = {}
        for name in wanted:
            rc, text = await self._amixer("sget", name)
            out[name] = self._parse_amixer(text) if rc == 0 else None
        return {"card": self._mixer_card, "controls": out}

    async def mixer_set(self, name: str, value: str) -> dict:
        rc, text = await self._amixer("sset", name, value)
        if rc != 0:
            raise ValueError(f"amixer rejected {name!r}={value!r}: {text.strip()}")
        return {"control": name, "value": self._parse_amixer(text) or value}

    async def mixer_apply_preset(self) -> dict:
        """Apply the measured WM8960 state. Missing controls are skipped, not
        fatal — driver revisions rename a few."""
        results: dict[str, str] = {}
        for name, value in MIXER_PRESET:
            rc, _ = await self._amixer("sset", name, value)
            results[name] = "ok" if rc == 0 else "skipped"
        return results

    async def mixer_store(self, path: str = "/var/lib/alsa/asound.state") -> dict:
        """Persist the mixer state across reboots.

        Needs root, and needs ``alsa-restore`` to be unmasked — the ReSpeaker
        installer masks it, which is why no setting survived a reboot before.
        If the call fails, the returned hint is the command to run by hand.
        """
        hint = f"sudo alsactl store -f {path}"
        if shutil.which("alsactl") is None:
            return {"stored": False, "reason": "alsactl not found", "hint": hint}
        proc = await asyncio.create_subprocess_exec(
            "alsactl", "store", "-f", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            return {"stored": True, "path": path}
        return {
            "stored": False,
            "reason": out.decode("utf-8", "replace").strip(),
            "hint": hint,
        }
