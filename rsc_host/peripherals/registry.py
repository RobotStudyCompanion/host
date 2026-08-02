"""Wire the peripheral layer to the dispatcher, event bus, and HAL backends.

Called once from :mod:`rsc_host.__main__` at startup. Responsible for:

  1. Constructing HAL backends per config (fake or pi).
  2. ``await backend.start()`` for each.
  3. Constructing peripherals atop the backends.
  4. Registering their verbs on the passed :class:`Dispatcher`.
  5. Wiring the arcade button's edge callback to publish events.

Returns a :class:`Peripherals` handle that can be ``stop()``'d cleanly on
shutdown.

Pin assignments track :mod:`rsc_host.peripherals` conventions and mirror the
thesis PCB layout; overrideable via constructor args for wiring quirks
(e.g. the test script's remap of the ring to M1_PWM=12).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.hal.base import (
    AudioBackend,
    GpioInputBackend,
    GpioPwmBackend,
    RingBackend,
    SerialBackend,
    ServoBackend,
)
from rsc_host.hal.fake import (
    FakeAudio,
    FakeGpioInput,
    FakeGpioPwm,
    FakeRing,
    FakeSerial,
    FakeServo,
)
from rsc_host.peripherals.button import ArcadeButton
from rsc_host.peripherals.button_led import ButtonLed
from rsc_host.peripherals.cyd import CydBridge, curated_cyd_verbs
from rsc_host.peripherals.flipper import Flipper
from rsc_host.peripherals.ring import Ring, registered_modes as ring_modes

log = logging.getLogger(__name__)


# ---- Wiring config ----


@dataclass(frozen=True, slots=True)
class Pinout:
    """Physical pin assignments.

    Defaults match the test script's tested wiring (ring on M1_PWM=12, flippers
    on M2/M3 =13/26). Overrideable at construction time.
    """

    flipper_left_pin: int = 13    # M2_PWM / J7
    flipper_right_pin: int = 26   # M3_PWM / J8 — used as right in the test rig
    flipper_m3_pin: int = 12      # M1_PWM / J6 — reserved for 3rd flipper (disabled)
    ring_pin: int = 12            # Same as m3 header on the test wiring
    ring_pixel_count: int = 16
    button_pin: int = 23
    button_led_pin: int = 24
    # M3 flipper is soft-disabled by default (hardware not fitted).
    m3_enabled: bool = False


@dataclass
class Peripherals:
    """Handle to running peripherals; used for lifecycle management."""

    servo_backend: ServoBackend
    ring_backend: RingBackend
    gpio_in_backend: GpioInputBackend
    gpio_pwm_backend: GpioPwmBackend
    serial_backend: SerialBackend
    audio_backend: AudioBackend

    flipper_left: Flipper
    flipper_right: Flipper
    flipper_m3: Flipper
    ring: Ring
    button: ArcadeButton
    button_led: ButtonLed
    cyd: CydBridge

    async def stop(self) -> None:
        """Stop peripherals in reverse order of construction, then backends."""
        await self.cyd.stop()
        await self.ring.stop()
        await self.button_led.stop()
        for f in (self.flipper_left, self.flipper_right, self.flipper_m3):
            try:
                await f.stop()
            except Exception:
                log.exception("flipper %s stop failed", f.id)
        # Backends last.
        for be in (
            self.serial_backend,
            self.audio_backend,
            self.gpio_pwm_backend,
            self.gpio_in_backend,
            self.ring_backend,
            self.servo_backend,
        ):
            try:
                await be.stop()
            except Exception:
                log.exception("backend %s stop failed", type(be).__name__)


# ---- Backend factory ----


def _build_backends(
    backend: str, pinout: Pinout
) -> tuple[
    ServoBackend, RingBackend, GpioInputBackend, GpioPwmBackend, SerialBackend, AudioBackend
]:
    if backend == "fake":
        return (
            FakeServo(),
            FakeRing(pixel_count=pinout.ring_pixel_count),
            FakeGpioInput(),
            FakeGpioPwm(),
            FakeSerial(),
            FakeAudio(),
        )
    if backend == "pi":
        # Imported lazily so laptop dev (backend=fake) doesn't need pigpio /
        # rpi_ws281x / neopixel / gpiozero installed.
        from rsc_host.hal.pi import (
            PiAudioBackend,
            PiGpioInputBackend,
            PiGpioPwmBackend,
            PiRingBackend,
            PiSerialBackend,
            PiServoBackend,
        )
        return (
            PiServoBackend(
                pins={
                    "left":  pinout.flipper_left_pin,
                    "right": pinout.flipper_right_pin,
                    "m3":    pinout.flipper_m3_pin,
                }
            ),
            PiRingBackend(pixel_count=pinout.ring_pixel_count),
            PiGpioInputBackend(pins=[pinout.button_pin]),
            PiGpioPwmBackend(pins=[pinout.button_led_pin]),
            PiSerialBackend(),
            PiAudioBackend(),
        )
    raise ValueError(f"unknown backend: {backend!r}")


# ---- Verb argument schemas ----
#
# Peripheral verbs live here rather than inside each peripheral module because
# they're wire-facing (pydantic) — the peripheral classes stay HAL-facing.


class _FlipperArgs(BaseModel):
    speed: float = Field(..., ge=-1.0, le=1.0)
    ramp_ms: int = Field(0, ge=0)


class _FlipperStopArgs(BaseModel):
    pass


class _RingModeArgs(BaseModel):
    mode: str
    params: dict[str, Any] = Field(default_factory=dict)


class _ButtonLedArgs(BaseModel):
    mode: str
    params: dict[str, Any] = Field(default_factory=dict)


class _CydCuratedArgs(BaseModel):
    """Args common to every curated cyd.* verb: an optional string value."""
    value: str | None = None


class _CydRawArgs(BaseModel):
    line: str


class _EmptyArgs(BaseModel):
    pass


# ---- Setup ----


async def setup(
    dispatcher: Dispatcher,
    bus: EventBus,
    backend: str = "fake",
    pinout: Pinout | None = None,
) -> Peripherals:
    """Build backends + peripherals, register verbs, start everything."""
    pinout = pinout or Pinout()

    # Backends
    servo_be, ring_be, gpio_in_be, gpio_pwm_be, serial_be, audio_be = _build_backends(
        backend, pinout
    )
    for be in (servo_be, ring_be, gpio_in_be, gpio_pwm_be, serial_be, audio_be):
        await be.start()

    # Peripherals
    flipper_left = Flipper("left", servo_be, bus)
    flipper_right = Flipper("right", servo_be, bus)
    flipper_m3 = Flipper("m3", servo_be, bus, enabled=pinout.m3_enabled)

    ring = Ring(ring_be, bus)
    button = ArcadeButton(pinout.button_pin, gpio_in_be, bus)
    button_led = ButtonLed(pinout.button_led_pin, gpio_pwm_be, bus)
    cyd = CydBridge(serial_be, bus)

    await button.start()
    await cyd.start()

    # ---- Verb registration ----

    def register_flipper(name: str, flipper: Flipper) -> None:
        @dispatcher.verb(f"flipper.{name}", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            await flipper.set_speed(args.speed, ramp_ms=args.ramp_ms)
            return {
                "id": flipper.id,
                "speed": args.speed,
                "enabled": flipper.enabled,
            }

        @dispatcher.verb(f"flipper.{name}.stop", args_model=_FlipperStopArgs)
        async def _stop_handler(_args: _FlipperStopArgs) -> dict:
            await flipper.stop()
            return {"id": flipper.id, "speed": 0.0}

    register_flipper("left", flipper_left)
    register_flipper("right", flipper_right)
    register_flipper("m3", flipper_m3)

    @dispatcher.verb("ring.mode", args_model=_RingModeArgs)
    async def _ring_mode_handler(args: _RingModeArgs) -> dict:
        try:
            await ring.set_mode(args.mode, args.params)
        except KeyError as exc:
            # Surface as an INVALID_ARGS-style failure via ValueError; the
            # dispatcher wraps it into INTERNAL_ERROR otherwise. Raising
            # ValueError doesn't currently map either — best just to return
            # a failure-shaped dict? No — cleanest is: raise so the client
            # sees INTERNAL_ERROR. But that's user-facing. Compromise:
            # translate KeyError into a plain-english exception.
            raise ValueError(str(exc)) from exc
        return {"mode": args.mode, "params": args.params}

    @dispatcher.verb("ring.modes", args_model=_EmptyArgs)
    async def _ring_modes_handler(_args: _EmptyArgs) -> dict:
        return {"modes": list(ring_modes())}

    @dispatcher.verb("button_led", args_model=_ButtonLedArgs)
    async def _button_led_handler(args: _ButtonLedArgs) -> dict:
        await button_led.set_mode(args.mode, args.params)
        return {"mode": args.mode}

    # CYD curated verbs — register each name in the map.
    def register_cyd(verb_name: str) -> None:
        @dispatcher.verb(verb_name, args_model=_CydCuratedArgs)
        async def _handler(args: _CydCuratedArgs) -> dict:
            await cyd.send_curated(verb_name, args.value)
            return {"verb": verb_name, "value": args.value}

    for verb_name in curated_cyd_verbs():
        register_cyd(verb_name)

    @dispatcher.verb("cyd.raw", args_model=_CydRawArgs)
    async def _cyd_raw_handler(args: _CydRawArgs) -> dict:
        await cyd.send_raw(args.line)
        return {"line": args.line}

    log.info(
        "peripherals ready: backend=%s, ring_modes=%s, cyd_verbs=%d",
        backend,
        ring_modes(),
        len(curated_cyd_verbs()),
    )

    return Peripherals(
        servo_backend=servo_be,
        ring_backend=ring_be,
        gpio_in_backend=gpio_in_be,
        gpio_pwm_backend=gpio_pwm_be,
        serial_backend=serial_be,
        audio_backend=audio_be,
        flipper_left=flipper_left,
        flipper_right=flipper_right,
        flipper_m3=flipper_m3,
        ring=ring,
        button=button,
        button_led=button_led,
        cyd=cyd,
    )
