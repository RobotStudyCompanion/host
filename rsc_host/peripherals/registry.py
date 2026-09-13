"""Wire the peripheral layer to the dispatcher, event bus, and HAL backends.

Called once from :mod:`rsc_host.__main__` at startup. Responsible for:

  1. Constructing HAL backends per config (fake or pi).
  2. ``await backend.start()`` for each.
  3. Constructing peripherals atop the backends.
  4. Registering their verbs on the passed :class:`Dispatcher`.
  5. Wiring the arcade button's edge callback to publish events.

Returns a :class:`Peripherals` handle that can be ``stop()``'d cleanly on
shutdown.

Pin ownership
-------------
GPIO 12 appears twice on this chassis: the J6 header carries both M1_PWM (a
third flipper position) and the NeoPixel ring's data line. Only one of them can
own the line. The third flipper is off by default, and the servo backend is
handed pins for *fitted* flippers only — claiming GPIO 12 for a servo nobody
installed makes the ring fail to initialise with a message about DMA rather
than about pin ownership, which is a long way to walk for a line no one uses.
:func:`setup` refuses to start with both enabled.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from rsc_host.config import AudioSettings, RingSettings, SerialSettings, ServoSettings
from rsc_host.errors import PeripheralUnavailableError
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
from rsc_host.peripherals.audio import Audio
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

    Defaults match the tested wiring: ring on M1_PWM/J6 = 12, flippers on
    M2/M3 = 13/26, arcade button on 23 with its LED on 24 through Q1.

    ``ring_pin`` and ``flipper_m3_pin`` are the same line on purpose — that is
    how the board is wired. ``m3_enabled`` decides which of them gets it, and
    the ring wins unless the third flipper is explicitly fitted.
    """

    flipper_left_pin: int = 13    # M2_PWM / J7
    flipper_right_pin: int = 26   # M3_PWM / J8 — used as right in the test rig
    flipper_m3_pin: int = 12      # M1_PWM / J6 — shared with the ring
    ring_pin: int = 12            # M1_PWM / J6; PWM0, which is why it needs root
    ring_pixel_count: int = 16
    button_pin: int = 23          # idles low, needs a pull-down
    button_led_pin: int = 24      # via Q1; the gate floats when the line frees
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
    audio: Audio

    async def stop(self) -> None:
        """Stop peripherals in reverse order of construction, then backends.

        Every step is isolated. One peripheral refusing to stop must not strand
        the rest — least of all leave a servo turning because the ring helper
        socket happened to be gone.
        """

        async def _quietly(what: str, coro) -> None:
            try:
                await coro
            except Exception:
                log.exception("failed to stop %s", what)

        await _quietly("audio capture", self.audio.stop_capture())
        await _quietly("audio playback", self.audio.stop_play())
        await _quietly("cyd", self.cyd.stop())
        await _quietly("ring", self.ring.stop())
        await _quietly("button led", self.button_led.stop())
        for flipper in (self.flipper_left, self.flipper_right, self.flipper_m3):
            await _quietly(f"flipper {flipper.id}", flipper.stop())
        # Belt and braces: cease every pulse train before the lines are freed.
        await _quietly("servos", self.servo_backend.stop_all())

        for be in (
            self.serial_backend,
            self.audio_backend,
            self.gpio_pwm_backend,
            self.gpio_in_backend,
            self.ring_backend,
            self.servo_backend,
        ):
            await _quietly(f"backend {type(be).__name__}", be.stop())


# ---- Backend factory ----


def _servo_pins(pinout: Pinout) -> dict[str, int]:
    """Pins for the flippers that are actually fitted.

    ``m3`` is included only when enabled, so the ring keeps GPIO 12 in the
    default build.
    """
    pins = {
        "left": pinout.flipper_left_pin,
        "right": pinout.flipper_right_pin,
    }
    if pinout.m3_enabled:
        pins["m3"] = pinout.flipper_m3_pin
    return pins


def _build_backends(
    backend: str,
    pinout: Pinout,
    audio: AudioSettings,
    servo: ServoSettings,
    ring: RingSettings,
    serial: SerialSettings,
) -> tuple[
    ServoBackend, RingBackend, GpioInputBackend, GpioPwmBackend,
    SerialBackend, AudioBackend,
]:
    if backend == "fake":
        # Imported lazily to keep import cost off the pi path too.
        from rsc_host.hal.fake import (
            FakeAudio,
            FakeGpioInput,
            FakeGpioPwm,
            FakeRing,
            FakeSerial,
            FakeServo,
        )

        return (
            FakeServo(),
            FakeRing(pixel_count=pinout.ring_pixel_count),
            FakeGpioInput(),
            FakeGpioPwm(),
            FakeSerial(),
            FakeAudio(),
        )

    if backend != "pi":
        raise ValueError(f"unknown backend: {backend!r} (expected 'fake' or 'pi')")

    # Imported lazily so laptop dev (backend=fake) needs neither lgpio,
    # rpi_ws281x, neopixel, gpiozero, nor numpy installed.
    from rsc_host.hal.dsp import CaptureConfig
    from rsc_host.hal.pi import (
        PiAudioBackend,
        PiGpioInputBackend,
        PiGpioPwmBackend,
        PiRingBackend,
        PiSerialBackend,
        PiServoBackend,
        ServoCalibration,
    )

    calibration = {
        "left": ServoCalibration(
            null_us=servo.left_null_us,
            span_us=servo.left_span_us,
            invert=servo.left_invert,
            min_us=servo.min_us,
            max_us=servo.max_us,
        ),
        "right": ServoCalibration(
            null_us=servo.right_null_us,
            span_us=servo.right_span_us,
            invert=servo.right_invert,
            min_us=servo.min_us,
            max_us=servo.max_us,
        ),
        "m3": ServoCalibration(
            null_us=servo.m3_null_us,
            span_us=servo.m3_span_us,
            invert=servo.m3_invert,
            min_us=servo.min_us,
            max_us=servo.max_us,
        ),
    }

    capture = CaptureConfig(
        device_rate=audio.device_rate,
        device_channels=audio.device_channels,
        frame_ms=audio.frame_ms,
        channel_mode=audio.channel_mode,
        dc_block=audio.dc_block,
        stream_rate=audio.stream_rate,
        hpf_hz=audio.hpf_hz,
        hpf_mode=audio.hpf_mode,
        gain_db=audio.gain_db,
        aec=audio.aec,
        aec_tail_ms=audio.aec_tail_ms,
        aec_delay_ms=audio.aec_delay_ms,
    )

    return (
        PiServoBackend(
            _servo_pins(pinout),
            calibration,
            chip=servo.gpiochip,
            deadband=servo.deadband,
            idle_ms=servo.idle_ms,
        ),
        PiRingBackend(
            pixel_count=pinout.ring_pixel_count,
            mode=ring.mode,
            socket_path=ring.socket,
            brightness=ring.brightness,
            gpio=pinout.ring_pin,
            white_mode=ring.white_mode,
        ),
        PiGpioInputBackend(pins=[pinout.button_pin]),
        PiGpioPwmBackend(pins=[pinout.button_led_pin]),
        PiSerialBackend(
            device=serial.device,
            baudrate=serial.baudrate,
            required=serial.required,
        ),
        PiAudioBackend(
            input_device=audio.input_device,
            output_device=audio.output_device,
            capture=capture,
            mixer_card=audio.mixer_card,
            apply_mixer_preset=audio.apply_mixer_preset,
        ),
    )


# ---- Verb argument schemas ----
#
# Peripheral verbs live here rather than inside each peripheral module because
# they're wire-facing (pydantic) — the peripheral classes stay HAL-facing.


class _FlipperArgs(BaseModel):
    speed: float = Field(..., ge=-1.0, le=1.0)
    ramp_ms: int = Field(0, ge=0, le=10_000)


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


class _AudioPlayUrlArgs(BaseModel):
    url: str = Field(max_length=2048)
    preempt: bool = False


class _AudioSelftestArgs(BaseModel):
    """Record a short clip, write it to disk, optionally play it straight back."""

    seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    playback: bool = True
    path: str = Field(default="/tmp/rsc_selftest.wav", max_length=512)


class _ServoCalibrationArgs(BaseModel):
    """Read calibration. Omit ``id`` for every servo."""

    id: str | None = None


class _ServoCalibrateArgs(BaseModel):
    """Adjust one servo's calibration. Omitted fields are left alone.

    ``null_us`` is where the servo is genuinely still; ``span_us`` is the
    deflection that means full speed, and is where a speed mismatch between two
    physical servos gets corrected. They are different quantities — changing
    one does not imply the other.
    """

    id: str
    null_us: int | None = Field(default=None, ge=500, le=2500)
    span_us: int | None = Field(default=None, ge=1, le=800)
    invert: bool | None = None


class _CaptureTuneArgs(BaseModel):
    """Live capture-chain changes. Omitted fields are left alone.

    Device rate, channel count and frame length are deliberately absent: they
    need the ALSA device reopened, so they belong in the unit file rather than
    on the wire.
    """

    channel_mode: str | None = Field(
        default=None, description="left | right | sum | diff | mono"
    )
    dc_block: bool | None = None
    stream_rate: int | None = Field(default=None, ge=8000, le=48000)
    hpf_hz: float | None = Field(default=None, ge=0.0, le=1000.0)
    hpf_mode: str | None = Field(default=None, description="movavg | butter | off")
    gain_db: float | None = Field(default=None, ge=-40.0, le=40.0)
    aec: str | None = Field(default=None, description="off | speex | webrtc")
    aec_tail_ms: int | None = Field(default=None, ge=0, le=1000)
    aec_delay_ms: int | None = Field(default=None, ge=0, le=1000)


class _MixerGetArgs(BaseModel):
    names: list[str] | None = Field(default=None, max_length=64)


class _MixerSetArgs(BaseModel):
    name: str = Field(max_length=128)
    value: str = Field(max_length=64)


class _MixerStoreArgs(BaseModel):
    path: str = Field(default="/var/lib/alsa/asound.state", max_length=512)


class _EmptyArgs(BaseModel):
    pass


# ---- Setup ----


async def setup(
    dispatcher: Dispatcher,
    bus: EventBus,
    backend: str = "fake",
    pinout: Pinout | None = None,
    *,
    audio_settings: AudioSettings | None = None,
    servo_settings: ServoSettings | None = None,
    ring_settings: RingSettings | None = None,
    serial_settings: SerialSettings | None = None,
) -> Peripherals:
    """Build backends + peripherals, register verbs, start everything.

    Args:
        dispatcher: verb registry to populate.
        bus:        event bus peripherals publish onto.
        backend:    ``"fake"`` or ``"pi"``.
        pinout:     pin assignments. Derived from the settings groups when
                    omitted, which is the normal path.
        audio_settings / servo_settings / ring_settings / serial_settings:
                    groups from :mod:`rsc_host.config`; defaults are the values
                    measured on the chassis.
    """
    audio_cfg = audio_settings or AudioSettings()
    servo_cfg = servo_settings or ServoSettings()
    ring_cfg = ring_settings or RingSettings()
    serial_cfg = serial_settings or SerialSettings()

    if pinout is None:
        pinout = Pinout(
            ring_pin=ring_cfg.gpio,
            ring_pixel_count=ring_cfg.pixels,
            m3_enabled=servo_cfg.m3_enabled,
        )

    if pinout.m3_enabled and pinout.flipper_m3_pin == pinout.ring_pin:
        raise ValueError(
            f"GPIO{pinout.ring_pin} cannot drive both the third flipper and the "
            "NeoPixel ring. Set RSC_HOST_M3_ENABLED=false, or move one of them "
            "with RSC_HOST_RING_GPIO — noting the ring needs a PWM-capable pin, "
            "and GPIO21 (PCM) would break I2S audio."
        )

    # Backends
    servo_be, ring_be, gpio_in_be, gpio_pwm_be, serial_be, audio_be = _build_backends(
        backend, pinout, audio_cfg, servo_cfg, ring_cfg, serial_cfg
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
    audio = Audio(audio_be, bus)

    await button.start()
    await cyd.start()

    flippers = {
        "left": flipper_left,
        "right": flipper_right,
        "m3": flipper_m3,
    }

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

    for _name, _flipper in flippers.items():
        register_flipper(_name, _flipper)

    @dispatcher.verb("flipper.stop_all", args_model=_EmptyArgs)
    async def _flipper_stop_all(_args: _EmptyArgs) -> dict:
        """Stop every flipper at once — the verb a panicking client wants."""
        await asyncio.gather(*(f.stop() for f in flippers.values()))
        return {"stopped": sorted(flippers)}

    # ---- Servo calibration ----

    @dispatcher.verb("servo.calibration", args_model=_ServoCalibrationArgs)
    async def _servo_calibration(args: _ServoCalibrationArgs) -> dict:
        return {"calibration": servo_be.calibration(args.id)}

    @dispatcher.verb("servo.calibrate", args_model=_ServoCalibrateArgs)
    async def _servo_calibrate(args: _ServoCalibrateArgs) -> dict:
        changes = {
            key: value
            for key, value in (
                ("null_us", args.null_us),
                ("span_us", args.span_us),
                ("invert", args.invert),
            )
            if value is not None
        }
        if not changes:
            raise ValueError(
                "nothing to change; pass at least one of null_us, span_us, invert"
            )
        updated = servo_be.set_calibration(args.id, **changes)
        return {
            "id": args.id,
            "calibration": updated,
            "persisted": False,
            "hint": (
                "in-memory only, and lost on restart. Once a second "
                "calibration run agrees, write "
                f"RSC_HOST_SERVO_{args.id.upper()}_NULL_US / _SPAN_US into the "
                "unit file."
            ),
        }

    # ---- Ring ----

    @dispatcher.verb("ring.mode", args_model=_RingModeArgs)
    async def _ring_mode_handler(args: _RingModeArgs) -> dict:
        # An unknown mode raises KeyError and an unavailable ring raises
        # PeripheralUnavailableError; the dispatcher maps both onto proper wire
        # codes, so neither needs translating here.
        await ring.set_mode(args.mode, args.params)
        return {"mode": args.mode, "params": args.params}

    @dispatcher.verb("ring.off", args_model=_EmptyArgs)
    async def _ring_off_handler(_args: _EmptyArgs) -> dict:
        await ring.stop()
        return {"mode": None}

    @dispatcher.verb("ring.modes", args_model=_EmptyArgs)
    async def _ring_modes_handler(_args: _EmptyArgs) -> dict:
        return {"modes": list(ring_modes())}

    @dispatcher.verb("ring.status", args_model=_EmptyArgs)
    async def _ring_status_handler(_args: _EmptyArgs) -> dict:
        status = dict(ring_be.status())
        status["current_mode"] = ring.current_mode
        return status

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

    # ---- Audio verbs ----

    @dispatcher.verb("audio.play_url", args_model=_AudioPlayUrlArgs)
    async def _audio_play_url(args: _AudioPlayUrlArgs) -> dict:
        # Fetch and play. Kept simple: download whole file, then play.
        # Streaming from URL directly to the sink is a later refinement.
        import urllib.request

        def _fetch() -> bytes:
            with urllib.request.urlopen(args.url, timeout=10) as resp:
                return resp.read()

        try:
            wav_bytes = await asyncio.to_thread(_fetch)
        except Exception as exc:
            raise ValueError(f"failed to fetch {args.url}: {exc}") from exc
        await audio.play(wav_bytes, preempt=args.preempt)
        return {"bytes": len(wav_bytes)}

    @dispatcher.verb("audio.stop_play", args_model=_EmptyArgs)
    async def _audio_stop_play(_args: _EmptyArgs) -> dict:
        await audio.stop_play()
        return {}

    @dispatcher.verb("audio.capture.stop", args_model=_EmptyArgs)
    async def _audio_capture_stop(_args: _EmptyArgs) -> dict:
        await audio.stop_capture()
        return {}

    @dispatcher.verb("audio.devices", args_model=_EmptyArgs)
    async def _audio_devices(_args: _EmptyArgs) -> dict:
        return await audio_be.list_devices()

    @dispatcher.verb("audio.selftest", args_model=_AudioSelftestArgs)
    async def _audio_selftest(args: _AudioSelftestArgs) -> dict:
        """Record, measure, write, play back — the whole audio path in one verb.

        Capture and playback are the only subsystems that cannot be checked by
        eye, and a deaf microphone produces a file of exactly the right length
        full of near-silence. Returning levels alongside the path means a dead
        input reads as a number rather than as a file nobody opens.

        Blocks for roughly ``seconds`` twice over when playback is on — once
        recording, once playing.
        """
        import math
        import wave

        import numpy as np

        fmt = audio.capture_format()
        rate = int(fmt["samplerate"])
        channels = int(fmt["channels"])
        width = int(fmt["sample_width"])

        queue = await audio.start_capture()
        chunks: list[bytes] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if frame == b"":
                    break  # sentinel: capture ended on its own
                chunks.append(frame)
        finally:
            await audio.stop_capture()

        pcm = b"".join(chunks)
        if not pcm:
            raise PeripheralUnavailableError(
                "capture produced no audio; check `arecord -l` and the mixer"
            )

        def _write_and_measure() -> dict:
            with wave.open(args.path, "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(width)
                w.setframerate(rate)
                w.writeframes(pcm)
            xs = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)

            def dbfs(v: float) -> float:
                return round(20 * math.log10(v / 32768), 1) if v > 0 else -120.0

            return {
                "peak_dbfs": dbfs(float(np.abs(xs).max())),
                "rms_dbfs": dbfs(float(np.sqrt((xs**2).mean()))),
            }

        levels = await asyncio.to_thread(_write_and_measure)

        frames = len(pcm) // (width * channels)
        result = {
            "path": args.path,
            "bytes": len(pcm),
            "seconds": round(frames / rate, 3),
            "format": fmt,
            "played": False,
            **levels,
        }

        if args.playback:
            def _read() -> bytes:
                with open(args.path, "rb") as fh:
                    return fh.read()

            wav_bytes = await asyncio.to_thread(_read)
            await audio.play(wav_bytes, preempt=True)
            result["played"] = True
        return result

    # ---- Capture chain tuning ----
    #
    # These expose the measured recipe as live controls rather than baking it
    # into the source: channel choice, DC removal, wire rate, high-pass corner,
    # make-up gain and the echo-canceller hook. A calibration session can walk
    # the same ground the test rig walks, against the running daemon.

    @dispatcher.verb("audio.capture.config", args_model=_EmptyArgs)
    async def _audio_capture_config(_args: _EmptyArgs) -> dict:
        return audio_be.capture_config()

    @dispatcher.verb("audio.capture.tune", args_model=_CaptureTuneArgs)
    async def _audio_capture_tune(args: _CaptureTuneArgs) -> dict:
        changes = args.model_dump(exclude_none=True)
        if not changes:
            raise ValueError("nothing to change; pass at least one parameter")
        # Rejects an invalid combination without disturbing the running chain,
        # so a bad tune costs a failed command rather than a capture session.
        config = audio_be.retune_capture(**changes)
        return config

    @dispatcher.verb("audio.capture.stats", args_model=_EmptyArgs)
    async def _audio_capture_stats(_args: _EmptyArgs) -> dict:
        """Live levels — the daemon's equivalent of ``rsc-test meter``.

        ``input_*`` is the device before processing, which is where a wrong
        analogue gain shows up; ``output_*`` is what reaches the wire.
        """
        return audio_be.capture_stats()

    @dispatcher.verb("audio.capture.stats.reset", args_model=_EmptyArgs)
    async def _audio_capture_stats_reset(_args: _EmptyArgs) -> dict:
        audio_be.reset_capture_stats()
        return {"reset": True}

    # ---- Mixer control ----
    #
    # Gain belongs in the analogue boost ahead of the ADC, not the digital
    # Capture control behind it. Boost lifts the signal relative to the noise
    # floor; Capture lifts both equally.

    @dispatcher.verb("audio.mixer.get", args_model=_MixerGetArgs)
    async def _audio_mixer_get(args: _MixerGetArgs) -> dict:
        return await audio_be.mixer_get(args.names)

    @dispatcher.verb("audio.mixer.set", args_model=_MixerSetArgs)
    async def _audio_mixer_set(args: _MixerSetArgs) -> dict:
        return await audio_be.mixer_set(args.name, args.value)

    @dispatcher.verb("audio.mixer.preset", args_model=_EmptyArgs)
    async def _audio_mixer_preset(_args: _EmptyArgs) -> dict:
        return {"applied": await audio_be.mixer_apply_preset()}

    @dispatcher.verb("audio.mixer.store", args_model=_MixerStoreArgs)
    async def _audio_mixer_store(args: _MixerStoreArgs) -> dict:
        return await audio_be.mixer_store(args.path)

    # ---- Status ----

    # Named peripherals.status, not status: __main__ owns the bare "status"
    # verb (version, verb list, subscriber count) and registering it twice
    # raises at boot.
    @dispatcher.verb("peripherals.status", args_model=_EmptyArgs)
    async def _peripherals_status(_args: _EmptyArgs) -> dict:
        """One-shot snapshot of every peripheral.

        Deliberately tolerant: a peripheral that cannot answer reports its
        error inline rather than failing the whole call, because the times you
        most want a status snapshot are the times something is broken.
        """

        def _safe(fn, *args):
            try:
                return fn(*args)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}

        return {
            "backend": backend,
            "flippers": {
                name: {
                    "speed": flipper.current_speed,
                    "enabled": flipper.enabled,
                }
                for name, flipper in flippers.items()
            },
            "servo_calibration": _safe(servo_be.calibration),
            "ring": {
                **_safe(ring_be.status),
                "current_mode": ring.current_mode,
            },
            "button_led": {"mode": button_led.current_mode},
            "cyd": {"available": getattr(serial_be, "available", True)},
            "audio": {
                "capturing": audio.is_capturing,
                "playing": audio.is_playing,
                "capture": _safe(audio_be.capture_config),
            },
            "pinout": {
                "flipper_left": pinout.flipper_left_pin,
                "flipper_right": pinout.flipper_right_pin,
                "flipper_m3": pinout.flipper_m3_pin if pinout.m3_enabled else None,
                "ring": pinout.ring_pin,
                "button": pinout.button_pin,
                "button_led": pinout.button_led_pin,
            },
        }

    log.info(
        "peripherals ready: backend=%s, servo_pins=%s, ring=%s, cyd_verbs=%d",
        backend,
        _servo_pins(pinout),
        ring_be.status(),
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
        audio=audio,
    )