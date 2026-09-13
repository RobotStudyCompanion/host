"""Runtime configuration for the host service.

Env-var driven; sensible defaults for laptop development. Production deployment
(the systemd unit on the Pi) sets these explicitly — see
``systemd/rsc-host@.service``.

Every hardware constant that was measured on the bench appears here as a
default, so a recalibration run changes the unit file rather than the source.

Core
    RSC_HOST_BIND               Interface to bind (default: 127.0.0.1)
    RSC_HOST_PORT               TCP port (default: 8765)
    RSC_HOST_TOKEN              Bearer token — REQUIRED, no default
    RSC_HOST_BACKEND            HAL backend: fake | pi (default: fake)
    RSC_HOST_TLS_CERT           PEM cert path; enables TLS if set
    RSC_HOST_TLS_KEY            PEM key path; enables TLS if set
    RSC_HOST_LOG_LEVEL          Python log level (default: INFO)
    RSC_HOST_ADVERTISE          LAN mDNS discovery (default: true)
    RSC_HOST_ROBOT_NAME         Override the advertised name (default: hostname)

Audio — device side (fixed by the hardware; changing needs a capture restart)
    RSC_HOST_AUDIO_INPUT        ALSA capture PCM     (default: plughw:0,0)
    RSC_HOST_AUDIO_OUTPUT       ALSA playback PCM    (default: plughw:0,0)
    RSC_HOST_AUDIO_DEVICE_RATE  Capture rate in Hz   (default: 48000)
    RSC_HOST_AUDIO_DEVICE_CHANNELS  Device channels  (default: 2)
    RSC_HOST_AUDIO_FRAME_MS     Period length in ms  (default: 20)

    48 kHz is not a preference. Asking this driver for 16 kHz through plughw
    produces audible warble; capture native and resample in software.

Audio — signal chain (all live-tunable via the audio.capture.tune verb)
    RSC_HOST_AUDIO_CHANNEL_MODE left | right | sum | diff | mono (default: left)
    RSC_HOST_AUDIO_DC_BLOCK     Remove the DC offset (default: true)
    RSC_HOST_AUDIO_STREAM_RATE  Wire rate in Hz, must divide the device rate
                                (default: 16000)
    RSC_HOST_AUDIO_HPF_HZ       High-pass corner, 0 disables (default: 80)
    RSC_HOST_AUDIO_HPF_MODE     movavg | butter | off (default: movavg)
    RSC_HOST_AUDIO_GAIN_DB      Digital make-up gain (default: 0)
    RSC_HOST_AUDIO_AEC          off | speex | webrtc (default: off)
    RSC_HOST_AUDIO_AEC_TAIL_MS  Echo tail length     (default: 150)
    RSC_HOST_AUDIO_AEC_DELAY_MS Playback-to-mic loop delay (default: 0)
    RSC_HOST_AUDIO_MIXER_CARD   amixer card index/name (default: 0)
    RSC_HOST_AUDIO_APPLY_MIXER  Apply the known-good WM8960 preset at start
                                (default: true)

Servos
    RSC_HOST_GPIOCHIP           gpiochip index (default: 0)
    RSC_HOST_SERVO_DEADBAND     |speed| below this counts as stopped (0.02)
    RSC_HOST_SERVO_IDLE_MS      Hold neutral this long before ceasing pulses
                                (default: 120). Ceasing pulses is worth ~5 dB
                                of speech-band SNR during capture.
    RSC_HOST_SERVO_MIN_US / _MAX_US   Safe pulse window (900 / 2100)
    RSC_HOST_SERVO_LEFT_NULL_US   (1495 — measured, clean plateau 1480-1510)
    RSC_HOST_SERVO_LEFT_SPAN_US   (100)
    RSC_HOST_SERVO_LEFT_INVERT    (false)
    RSC_HOST_SERVO_RIGHT_NULL_US  (1510 — PROVISIONAL, wants a quiet re-run)
    RSC_HOST_SERVO_RIGHT_SPAN_US  (205 — 100 plus the 105 µs speed-match trim)
    RSC_HOST_SERVO_RIGHT_INVERT   (true — mounted mirrored)
    RSC_HOST_SERVO_M3_NULL_US / _SPAN_US / _INVERT
    RSC_HOST_M3_ENABLED           Third flipper fitted? (default: false)

    Null and span are different quantities. Null is where the servo is
    genuinely still; span is how far from null full speed sits, and is where a
    speed mismatch between two physical servos is corrected. Do not conflate
    them.

Ring
    RSC_HOST_RING_MODE          auto | helper | direct | off (default: auto)
    RSC_HOST_RING_SOCKET        Helper socket (default: /run/rsc/ring.sock)
    RSC_HOST_RING_PIXELS        Pixel count (default: 16)
    RSC_HOST_RING_GPIO          BCM pin (default: 12 — do not move to 21,
                                that is PCM, which the I2S codec needs)
    RSC_HOST_RING_BRIGHTNESS    0.0-1.0 (default: 0.3)
    RSC_HOST_RING_WHITE_MODE    extract | off (default: extract)

CYD serial
    RSC_HOST_SERIAL_DEVICE      (default: /dev/serial0)
    RSC_HOST_SERIAL_BAUD        (default: 115200)
    RSC_HOST_SERIAL_REQUIRED    Fail startup if the UART will not open
                                (default: false)

The token has no default: laptop dev must set ``RSC_HOST_TOKEN=dev``
explicitly. This prevents accidental production runs with a guessable secret.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

Backend = Literal["fake", "pi"]

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


# -----------------------------------------------------------------------------
# Env parsing helpers
# -----------------------------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in choices:
        raise ValueError(f"{name} must be one of {choices}, got {raw!r}")
    return raw


# -----------------------------------------------------------------------------
# Setting groups
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioSettings:
    """Capture and playback configuration.

    The device-side fields describe hardware and cannot be retuned live; the
    chain fields all can, via the ``audio.capture.tune`` verb.
    """

    input_device: str = "plughw:0,0"
    output_device: str = "plughw:0,0"
    device_rate: int = 48000
    device_channels: int = 2
    frame_ms: int = 20

    channel_mode: str = "left"
    dc_block: bool = True
    stream_rate: int = 16000
    hpf_hz: float = 80.0
    hpf_mode: str = "movavg"
    gain_db: float = 0.0

    aec: str = "off"
    aec_tail_ms: int = 150
    aec_delay_ms: int = 0

    mixer_card: str = "0"
    apply_mixer_preset: bool = True

    @classmethod
    def from_env(cls) -> "AudioSettings":
        # Deprecated aliases from before the capture and stream rates were
        # separated. Honoured with a warning so existing units keep working.
        legacy_rate = os.environ.get("RSC_HOST_AUDIO_SAMPLERATE", "").strip()
        stream_default = 16000
        if legacy_rate:
            log.warning(
                "RSC_HOST_AUDIO_SAMPLERATE is deprecated; it now sets the "
                "*stream* rate. Capture is always at RSC_HOST_AUDIO_DEVICE_RATE "
                "(48000 on this hardware). Use RSC_HOST_AUDIO_STREAM_RATE."
            )
            try:
                stream_default = int(legacy_rate)
            except ValueError as exc:
                raise ValueError(
                    f"RSC_HOST_AUDIO_SAMPLERATE must be an integer: {exc}"
                ) from exc
        if os.environ.get("RSC_HOST_AUDIO_CHANNELS", "").strip():
            log.warning(
                "RSC_HOST_AUDIO_CHANNELS is deprecated and ignored. The device "
                "is always opened with RSC_HOST_AUDIO_DEVICE_CHANNELS channels; "
                "which one reaches the wire is RSC_HOST_AUDIO_CHANNEL_MODE."
            )

        return cls(
            input_device=_env_str("RSC_HOST_AUDIO_INPUT", "plughw:0,0"),
            output_device=_env_str("RSC_HOST_AUDIO_OUTPUT", "plughw:0,0"),
            device_rate=_env_int("RSC_HOST_AUDIO_DEVICE_RATE", 48000, minimum=8000),
            device_channels=_env_int("RSC_HOST_AUDIO_DEVICE_CHANNELS", 2, minimum=1),
            frame_ms=_env_int("RSC_HOST_AUDIO_FRAME_MS", 20, minimum=1),
            channel_mode=_env_choice(
                "RSC_HOST_AUDIO_CHANNEL_MODE", "left",
                ("left", "right", "sum", "diff", "mono"),
            ),
            dc_block=_env_bool("RSC_HOST_AUDIO_DC_BLOCK", True),
            stream_rate=_env_int(
                "RSC_HOST_AUDIO_STREAM_RATE", stream_default, minimum=1
            ),
            hpf_hz=_env_float("RSC_HOST_AUDIO_HPF_HZ", 80.0, minimum=0.0),
            hpf_mode=_env_choice(
                "RSC_HOST_AUDIO_HPF_MODE", "movavg", ("movavg", "butter", "off")
            ),
            gain_db=_env_float("RSC_HOST_AUDIO_GAIN_DB", 0.0),
            aec=_env_choice("RSC_HOST_AUDIO_AEC", "off", ("off", "speex", "webrtc")),
            aec_tail_ms=_env_int("RSC_HOST_AUDIO_AEC_TAIL_MS", 150, minimum=0),
            aec_delay_ms=_env_int("RSC_HOST_AUDIO_AEC_DELAY_MS", 0, minimum=0),
            mixer_card=_env_str("RSC_HOST_AUDIO_MIXER_CARD", "0"),
            apply_mixer_preset=_env_bool("RSC_HOST_AUDIO_APPLY_MIXER", True),
        )

    def validate(self) -> "AudioSettings":
        if self.device_channels not in (1, 2):
            raise ValueError(
                f"RSC_HOST_AUDIO_DEVICE_CHANNELS must be 1 or 2, "
                f"got {self.device_channels}"
            )
        if self.device_rate % self.stream_rate != 0:
            valid = [
                self.device_rate // f
                for f in range(1, 13)
                if self.device_rate % f == 0 and self.device_rate // f >= 8000
            ]
            raise ValueError(
                f"RSC_HOST_AUDIO_STREAM_RATE ({self.stream_rate}) must divide "
                f"RSC_HOST_AUDIO_DEVICE_RATE ({self.device_rate}) exactly. "
                f"Valid values: {valid}"
            )
        frame_samples = int(self.device_rate * self.frame_ms / 1000)
        factor = self.device_rate // self.stream_rate
        if frame_samples % factor != 0:
            raise ValueError(
                f"RSC_HOST_AUDIO_FRAME_MS ({self.frame_ms}) gives "
                f"{frame_samples} samples, which is not a multiple of the "
                f"{factor}:1 decimation"
            )
        if self.device_channels == 1 and self.channel_mode in ("right", "sum", "diff"):
            raise ValueError(
                f"RSC_HOST_AUDIO_CHANNEL_MODE={self.channel_mode!r} needs "
                "2 device channels"
            )
        return self


@dataclass(frozen=True, slots=True)
class ServoSettings:
    """Pulse-width calibration and idle behaviour.

    Defaults are the values measured on the RSC chassis. The right servo's
    null is provisional — two runs in a noisy room gave 1505 and 1520, whose
    overlap suggests 1510. One confirming ``servo_null`` run in quiet
    conditions would settle it.
    """

    gpiochip: int = 0
    deadband: float = 0.02
    idle_ms: int = 120
    min_us: int = 900
    max_us: int = 2100

    left_null_us: int = 1495
    left_span_us: int = 100
    left_invert: bool = False

    right_null_us: int = 1510
    right_span_us: int = 205
    right_invert: bool = True

    m3_null_us: int = 1500
    m3_span_us: int = 100
    m3_invert: bool = False
    m3_enabled: bool = False

    @classmethod
    def from_env(cls) -> "ServoSettings":
        return cls(
            gpiochip=_env_int("RSC_HOST_GPIOCHIP", 0, minimum=0),
            deadband=_env_float("RSC_HOST_SERVO_DEADBAND", 0.02, minimum=0.0),
            idle_ms=_env_int("RSC_HOST_SERVO_IDLE_MS", 120, minimum=0),
            min_us=_env_int("RSC_HOST_SERVO_MIN_US", 900, minimum=500),
            max_us=_env_int("RSC_HOST_SERVO_MAX_US", 2100, minimum=1000),
            left_null_us=_env_int("RSC_HOST_SERVO_LEFT_NULL_US", 1495),
            left_span_us=_env_int("RSC_HOST_SERVO_LEFT_SPAN_US", 100, minimum=1),
            left_invert=_env_bool("RSC_HOST_SERVO_LEFT_INVERT", False),
            right_null_us=_env_int("RSC_HOST_SERVO_RIGHT_NULL_US", 1510),
            right_span_us=_env_int("RSC_HOST_SERVO_RIGHT_SPAN_US", 205, minimum=1),
            right_invert=_env_bool("RSC_HOST_SERVO_RIGHT_INVERT", True),
            m3_null_us=_env_int("RSC_HOST_SERVO_M3_NULL_US", 1500),
            m3_span_us=_env_int("RSC_HOST_SERVO_M3_SPAN_US", 100, minimum=1),
            m3_invert=_env_bool("RSC_HOST_SERVO_M3_INVERT", False),
            m3_enabled=_env_bool("RSC_HOST_M3_ENABLED", False),
        )


@dataclass(frozen=True, slots=True)
class RingSettings:
    """NeoPixel ring wiring and privilege strategy.

    GPIO 12 selects PWM0 in Blinka's backend. GPIO 21 would select PCM, which
    is the I2S clock the WM8960 uses — moving the ring there kills audio.
    """

    mode: str = "auto"
    socket: str = "/run/rsc/ring.sock"
    pixels: int = 16
    gpio: int = 12
    brightness: float = 0.3
    white_mode: str = "extract"

    @classmethod
    def from_env(cls) -> "RingSettings":
        return cls(
            mode=_env_choice(
                "RSC_HOST_RING_MODE", "auto", ("auto", "helper", "direct", "off")
            ),
            socket=_env_str("RSC_HOST_RING_SOCKET", "/run/rsc/ring.sock"),
            pixels=_env_int("RSC_HOST_RING_PIXELS", 16, minimum=1),
            gpio=_env_int("RSC_HOST_RING_GPIO", 12, minimum=0),
            brightness=_env_float("RSC_HOST_RING_BRIGHTNESS", 0.3, minimum=0.0),
            white_mode=_env_choice(
                "RSC_HOST_RING_WHITE_MODE", "extract", ("extract", "off")
            ),
        )


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """CYD front-panel UART."""

    device: str = "/dev/serial0"
    baudrate: int = 115200
    required: bool = False

    @classmethod
    def from_env(cls) -> "SerialSettings":
        return cls(
            device=_env_str("RSC_HOST_SERIAL_DEVICE", "/dev/serial0"),
            baudrate=_env_int("RSC_HOST_SERIAL_BAUD", 115200, minimum=1),
            required=_env_bool("RSC_HOST_SERIAL_REQUIRED", False),
        )


# -----------------------------------------------------------------------------
# Top-level config
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration."""

    bind: str
    port: int
    token: str
    backend: Backend
    tls_cert: str | None
    tls_key: str | None
    log_level: str
    advertise: bool
    robot_name: str | None
    audio: AudioSettings = field(default_factory=AudioSettings)
    servo: ServoSettings = field(default_factory=ServoSettings)
    ring: RingSettings = field(default_factory=RingSettings)
    serial: SerialSettings = field(default_factory=SerialSettings)

    @property
    def tls_enabled(self) -> bool:
        """TLS is enabled iff *both* cert and key paths are set."""
        return self.tls_cert is not None and self.tls_key is not None

    # Backwards-compatible accessors for call sites that predate the grouping.
    @property
    def audio_input(self) -> str:
        return self.audio.input_device

    @property
    def audio_output(self) -> str:
        return self.audio.output_device

    @property
    def audio_samplerate(self) -> int:
        return self.audio.stream_rate

    @property
    def audio_channels(self) -> int:
        return 1  # the chain always emits mono


def load_from_env() -> Config:
    """Read configuration from environment variables.

    Raises:
        RuntimeError: if RSC_HOST_TOKEN is unset or empty.
        ValueError:   on any malformed or inconsistent value.
    """
    token = os.environ.get("RSC_HOST_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "RSC_HOST_TOKEN is not set. Refusing to start without an auth token. "
            "For laptop dev, set RSC_HOST_TOKEN=dev explicitly."
        )

    backend_raw = _env_choice("RSC_HOST_BACKEND", "fake", ("fake", "pi"))

    tls_cert = os.environ.get("RSC_HOST_TLS_CERT", "").strip() or None
    tls_key = os.environ.get("RSC_HOST_TLS_KEY", "").strip() or None
    if bool(tls_cert) != bool(tls_key):
        raise ValueError(
            "RSC_HOST_TLS_CERT and RSC_HOST_TLS_KEY must be set together, "
            "or both unset."
        )

    advertise = _env_bool("RSC_HOST_ADVERTISE", True)
    bind = _env_str("RSC_HOST_BIND", "127.0.0.1")
    if advertise and bind in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "RSC_HOST_ADVERTISE is on but RSC_HOST_BIND=%s only accepts local "
            "connections. mDNS will publish a LAN address nothing can reach. "
            "Set RSC_HOST_BIND=0.0.0.0 on the Pi.",
            bind,
        )

    config = Config(
        bind=bind,
        port=_env_int("RSC_HOST_PORT", 8765, minimum=1),
        token=token,
        backend=backend_raw,  # type: ignore[arg-type]  # narrowed above
        tls_cert=tls_cert,
        tls_key=tls_key,
        log_level=_env_str("RSC_HOST_LOG_LEVEL", "INFO").upper(),
        advertise=advertise,
        robot_name=os.environ.get("RSC_HOST_ROBOT_NAME", "").strip() or None,
        audio=AudioSettings.from_env().validate(),
        servo=ServoSettings.from_env(),
        ring=RingSettings.from_env(),
        serial=SerialSettings.from_env(),
    )
    return config
