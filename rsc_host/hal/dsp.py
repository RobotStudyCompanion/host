"""Capture-side DSP: the measured recipe, implemented as a streaming chain.

This module touches no hardware. It exists so the signal path can be unit
tested against synthetic audio, and so the fake and real backends can share
one implementation.

The chain implements the recipe verified on the RSC hardware:

  device (48 kHz, 2 ch, s16le)
      → channel select      (left only — summing L+R measured +1.0 dB, not worth it)
      → DC block            (offset measured at +440..900 LSB, costs ~5 dB headroom)
      → decimate            (48 k → 16 k, 3:1; the anti-alias filter also removes
                             the DC-DC converter's aliased tones above 6 kHz)
      → high-pass @ 80 Hz   (sub-speech-band energy, no intelligibility cost)
      → AEC                 (scaffolded; full duplex needs it)
      → gain / clip
      → s16le mono at the stream rate

Every stage is optional and every parameter is live-tunable — see
:meth:`CaptureChain.retune`. Retuning rebuilds only the stages whose
parameters changed, so filter state survives an unrelated tweak.

**Ordering rationale.** DC removal comes before decimation because a large
offset makes the anti-alias filter's transient ugly. The high-pass comes
*after* decimation because it is an order of magnitude cheaper at 16 kHz, and
because rumble sits far below Nyquist/3 and therefore cannot alias on the way
down. The AEC sits after the high-pass so the canceller sees the same
band-limited signal the far end will.

**Dependencies.** numpy only. scipy is used for the Butterworth high-pass if
it happens to be installed, but the default ``movavg`` mode needs nothing
beyond numpy and is what runs on the Pi.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, replace
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

_INT16_FULL_SCALE = 32768.0

CHANNEL_MODES = ("left", "right", "sum", "diff", "mono")
HPF_MODES = ("movavg", "butter", "off")
AEC_MODES = ("off", "speex", "webrtc")


def dbfs(value: float) -> float:
    """Amplitude in LSB → dBFS. Returns −120.0 for silence rather than −inf."""
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value / _INT16_FULL_SCALE)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """Everything the capture chain needs to know.

    ``device_rate`` and ``device_channels`` describe the hardware and are not
    negotiable at runtime — changing them means reopening the ALSA device.
    Everything else can be retuned live.
    """

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

    def __post_init__(self) -> None:
        if self.device_rate <= 0:
            raise ValueError(f"device_rate must be > 0, got {self.device_rate}")
        if self.device_channels not in (1, 2):
            raise ValueError(
                f"device_channels must be 1 or 2, got {self.device_channels}"
            )
        if self.channel_mode not in CHANNEL_MODES:
            raise ValueError(
                f"channel_mode must be one of {CHANNEL_MODES}, "
                f"got {self.channel_mode!r}"
            )
        if self.hpf_mode not in HPF_MODES:
            raise ValueError(
                f"hpf_mode must be one of {HPF_MODES}, got {self.hpf_mode!r}"
            )
        if self.aec not in AEC_MODES:
            raise ValueError(f"aec must be one of {AEC_MODES}, got {self.aec!r}")
        if self.device_channels == 1 and self.channel_mode in ("right", "sum", "diff"):
            raise ValueError(
                f"channel_mode={self.channel_mode!r} needs 2 device channels"
            )
        if self.stream_rate <= 0 or self.device_rate % self.stream_rate != 0:
            raise ValueError(
                f"stream_rate must divide device_rate exactly; "
                f"{self.device_rate} / {self.stream_rate} is not an integer. "
                f"Valid rates: {sorted(self.valid_stream_rates(self.device_rate))}"
            )
        if self.frame_samples % self.decimation != 0:
            raise ValueError(
                f"frame_ms={self.frame_ms} gives {self.frame_samples} samples, "
                f"which is not a multiple of the {self.decimation}:1 decimation. "
                f"Pick a frame_ms that divides evenly."
            )

    @staticmethod
    def valid_stream_rates(device_rate: int) -> tuple[int, ...]:
        """Stream rates that divide ``device_rate`` exactly and land in a
        sensible range for speech (≥ 8 kHz)."""
        return tuple(
            device_rate // f
            for f in range(1, 13)
            if device_rate % f == 0 and device_rate // f >= 8000
        )

    @property
    def decimation(self) -> int:
        """Integer decimation factor, device → stream."""
        return self.device_rate // self.stream_rate

    @property
    def frame_samples(self) -> int:
        """Frames (not bytes) per device period."""
        return int(self.device_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        """Bytes per device period, as read from the capture device."""
        return self.frame_samples * self.device_channels * 2

    @property
    def output_frame_samples(self) -> int:
        """Mono samples emitted per device period."""
        return self.frame_samples // self.decimation

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_rate": self.device_rate,
            "device_channels": self.device_channels,
            "frame_ms": self.frame_ms,
            "channel_mode": self.channel_mode,
            "dc_block": self.dc_block,
            "stream_rate": self.stream_rate,
            "decimation": self.decimation,
            "hpf_hz": self.hpf_hz,
            "hpf_mode": self.hpf_mode,
            "gain_db": self.gain_db,
            "aec": self.aec,
            "aec_tail_ms": self.aec_tail_ms,
            "aec_delay_ms": self.aec_delay_ms,
            "frame_bytes": self.frame_bytes,
            "output_frame_samples": self.output_frame_samples,
        }


# -----------------------------------------------------------------------------
# Primitives
# -----------------------------------------------------------------------------


class _Delay:
    """Pure sample delay, stateful across blocks."""

    __slots__ = ("_n", "_buf")

    def __init__(self, n: int) -> None:
        self._n = max(0, int(n))
        self._buf = np.zeros(self._n, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._n == 0:
            return x
        buf = np.concatenate((self._buf, x))
        self._buf = buf[len(x):].copy()
        return buf[: len(x)]


class _MovingAverage:
    """Boxcar low-pass computed by prefix sums — O(n), fully vectorised.

    An IIR filter would need a per-sample Python loop, which at 48 kHz is a
    meaningful slice of a Pi 4 core. A moving average has an exact zero at DC
    and at every multiple of ``fs/L``, is linear phase, and costs one cumsum.
    """

    __slots__ = ("_L", "_hist")

    def __init__(self, length: int) -> None:
        self._L = max(1, int(length))
        self._hist = np.zeros(self._L - 1, dtype=np.float64)

    @property
    def group_delay(self) -> int:
        return (self._L - 1) // 2

    def process(self, x: np.ndarray) -> np.ndarray:
        L = self._L
        if L <= 1:
            return x.astype(np.float32, copy=False)
        n = len(x)
        buf = np.concatenate((self._hist, x.astype(np.float64, copy=False)))
        # cs[i] = sum(buf[:i]); sum(buf[j:j+L]) == cs[j+L] - cs[j]
        cs = np.concatenate(([0.0], np.cumsum(buf)))
        out = (cs[L : L + n] - cs[:n]) / L
        self._hist = buf[-(L - 1):]
        return out.astype(np.float32)


class HighPass(Protocol):
    """Stateful high-pass filter over successive blocks."""

    latency_samples: int

    def process(self, x: np.ndarray) -> np.ndarray: ...


class MovingAverageHighPass:
    """Linear-phase high-pass built as ``delayed(x) − lowpass(x)``.

    Two cascaded boxcars give roughly −26 dB in the stopband sidelobes, which
    is ample for removing sub-speech rumble and DC drift. The corner is
    approximate — the cascade pulls it below the nominal ``fc``. Use
    ``hpf_mode='butter'`` when the exact corner matters and scipy is present.
    """

    def __init__(self, fs: int, fc: float, stages: int = 2) -> None:
        if fc <= 0:
            raise ValueError("fc must be > 0")
        length = max(3, int(round(fs / fc)))
        if length % 2 == 0:
            length += 1
        self._stages = [_MovingAverage(length) for _ in range(stages)]
        self.latency_samples = sum(s.group_delay for s in self._stages)
        self._delay = _Delay(self.latency_samples)
        self.length = length

    def process(self, x: np.ndarray) -> np.ndarray:
        lp = x
        for stage in self._stages:
            lp = stage.process(lp)
        return self._delay.process(x) - lp


class ButterworthHighPass:
    """Second-order Butterworth via scipy's stateful ``sosfilt``.

    Sharper corner than the boxcar cascade and near-zero latency, at the cost
    of a scipy dependency and a non-linear phase response. Only constructed
    when ``hpf_mode='butter'``.
    """

    def __init__(self, fs: int, fc: float, order: int = 2) -> None:
        from scipy import signal  # noqa: PLC0415 — optional dependency

        self._signal = signal
        self._sos = signal.butter(order, fc, btype="highpass", fs=fs, output="sos")
        self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)
        self.latency_samples = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = self._signal.sosfilt(self._sos, x, zi=self._zi)
        return y.astype(np.float32)


def make_highpass(fs: int, fc: float, mode: str) -> HighPass | None:
    """Build a high-pass, degrading to the numpy implementation if scipy is
    absent. Returns None when the filter is disabled."""
    if mode == "off" or fc <= 0:
        return None
    if mode == "butter":
        try:
            return ButterworthHighPass(fs, fc)
        except ImportError:
            log.warning(
                "hpf_mode='butter' needs scipy, which is not installed; "
                "falling back to 'movavg'"
            )
    return MovingAverageHighPass(fs, fc)


def design_lowpass(numtaps: int, cutoff_ratio: float) -> np.ndarray:
    """Windowed-sinc low-pass. ``cutoff_ratio`` is the corner as a fraction of
    the sample rate (so 0.15 at 48 kHz is 7.2 kHz)."""
    if numtaps % 2 == 0:
        numtaps += 1
    n = np.arange(numtaps, dtype=np.float64) - (numtaps - 1) / 2.0
    h = 2.0 * cutoff_ratio * np.sinc(2.0 * cutoff_ratio * n)
    h *= np.blackman(numtaps)
    h /= h.sum()
    return h.astype(np.float32)


class FirDecimator:
    """Streaming polyphase decimator.

    Only the samples that survive decimation are computed, so the cost scales
    with the *output* rate. At 3:1 with 181 taps that is under 3 MMAC/s — noise
    against everything else the daemon does.

    Phase is tracked across blocks, so an arbitrary block size still produces a
    continuous output stream.

    Filter defaults, measured rather than guessed. Everything above the output
    Nyquist folds back into the audible band, so stopband rejection is the only
    number that matters:

        121 taps @ 0.45 → −39 dB above 8 kHz   (audibly rough)
        181 taps @ 0.40 → −83 dB above 8 kHz   (current default)
        241 taps @ 0.40 → −90 dB above 8 kHz   (diminishing returns)

    The original 121/0.45 also drooped 3 dB at 7 kHz, inside the passband. The
    extra 60 taps cost about 1 MMAC/s and 1.25 ms of group delay.
    """

    def __init__(self, factor: int, numtaps: int = 181, cutoff: float = 0.40) -> None:
        if factor < 1:
            raise ValueError(f"factor must be >= 1, got {factor}")
        self.factor = factor
        self.h = design_lowpass(numtaps, cutoff / factor)
        self._hr = self.h[::-1].copy()
        self._M = len(self.h)
        self._hist = np.zeros(self._M - 1, dtype=np.float32)
        self._consumed = 0
        self.latency_samples = (self._M - 1) // 2

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.factor == 1:
            return x
        n = len(x)
        buf = np.concatenate((self._hist, x))
        # Global index of x[0] is self._consumed; keep outputs where the global
        # index is a multiple of the factor.
        first = (-self._consumed) % self.factor
        if first < n:
            idx = np.arange(first, n, self.factor)
            windows = np.lib.stride_tricks.sliding_window_view(buf, self._M)
            out = windows[idx] @ self._hr
        else:
            out = np.zeros(0, dtype=np.float32)
        self._hist = buf[-(self._M - 1):].copy()
        self._consumed += n
        return out.astype(np.float32, copy=False)


class DcBlocker:
    """Tracks and subtracts the capture path's DC offset.

    The WM8960 on this HAT sits at a consistent +440 to +900 LSB. A slow EMA
    over per-block means converges in a few hundred milliseconds and then
    barely moves, which is what we want: the offset is a property of the
    hardware, not of the signal.
    """

    __slots__ = ("_mean", "_alpha")

    def __init__(self, alpha: float = 0.05) -> None:
        self._mean: float | None = None
        self._alpha = alpha

    @property
    def offset(self) -> float:
        return self._mean or 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        block_mean = float(x.mean()) if len(x) else 0.0
        if self._mean is None:
            self._mean = block_mean  # converge instantly on the first block
        else:
            self._mean = (1.0 - self._alpha) * self._mean + self._alpha * block_mean
        return x - np.float32(self._mean)


# -----------------------------------------------------------------------------
# Acoustic echo cancellation — scaffolding
# -----------------------------------------------------------------------------


class ReferenceTap:
    """Ring buffer of what was most recently handed to the speaker.

    Written from whichever context drives playback, read from the capture
    chain, hence the lock. Stores mono float32 at the AEC rate.

    The tap is filled as chunks are *written to the sink*, so it leads the
    microphone by the ALSA output buffer plus the acoustic flight time. That
    offset is what ``aec_delay_ms`` compensates for; measure it once by
    correlating a played click against the captured one.
    """

    def __init__(self, rate: int, seconds: float = 4.0) -> None:
        self.rate = rate
        self._buf = np.zeros(int(rate * seconds), dtype=np.float32)
        self._write = 0
        self._written = 0
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._buf[:] = 0.0
            self._write = 0
            self._written = 0

    def push(self, mono: np.ndarray) -> None:
        """Append mono float32 samples already at :attr:`rate`."""
        if len(mono) == 0:
            return
        with self._lock:
            size = len(self._buf)
            data = mono[-size:] if len(mono) > size else mono
            end = self._write + len(data)
            if end <= size:
                self._buf[self._write : end] = data
            else:
                split = size - self._write
                self._buf[self._write :] = data[:split]
                self._buf[: end - size] = data[split:]
            self._write = end % size
            self._written += len(data)

    def take(self, count: int, delay_samples: int = 0) -> np.ndarray:
        """The ``count`` samples ending ``delay_samples`` before the write head.

        Returns zeros when the tap has not been filled that far — silence is
        the correct far-end estimate when nothing has been played.
        """
        out = np.zeros(count, dtype=np.float32)
        with self._lock:
            size = len(self._buf)
            available = min(self._written, size)
            need = count + delay_samples
            if available < need:
                return out
            end = (self._write - delay_samples) % size
            start = (end - count) % size
            if start < end:
                out[:] = self._buf[start:end]
            else:
                split = size - start
                out[:split] = self._buf[start:]
                out[split:] = self._buf[: end]
        return out


class AecStage(Protocol):
    name: str

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray: ...
    def close(self) -> None: ...


class NullAec:
    """Pass-through. The default until an implementation is proven on hardware."""

    name = "off"

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        return near

    def close(self) -> None:
        pass


class SpeexAec:
    """speexdsp echo canceller — **scaffold, not yet validated on hardware**.

    Needs ``speexdsp-python``. Operates on int16 frames of exactly
    ``frame_samples``; the chain guarantees that, since the frame size is
    derived from the device period.

    Known limitation: speex cancels only what it can predict from the far-end
    signal, so it will not touch the DC-DC converter's switching noise. That
    noise sits above 6 kHz and is removed by decimation anyway.
    """

    name = "speex"

    def __init__(self, rate: int, frame_samples: int, tail_ms: int) -> None:
        from speexdsp import EchoCanceller  # noqa: PLC0415 — optional dependency

        filter_length = max(frame_samples, int(rate * tail_ms / 1000))
        self._ec = EchoCanceller.create(frame_samples, filter_length, rate)
        self._frame = frame_samples
        log.info(
            "SpeexAec ready (rate=%d, frame=%d, tail=%d ms, filter=%d)",
            rate, frame_samples, tail_ms, filter_length,
        )

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        if len(near) != self._frame:
            # Ragged frame (start-up or shutdown) — pass it through untouched
            # rather than desynchronising the canceller's internal state.
            return near
        near_i16 = np.clip(near, -32768, 32767).astype(np.int16)
        far_i16 = np.clip(far, -32768, 32767).astype(np.int16)
        out = self._ec.process(near_i16.tobytes(), far_i16.tobytes())
        return np.frombuffer(out, dtype=np.int16).astype(np.float32)

    def close(self) -> None:
        self._ec = None


def make_aec(mode: str, rate: int, frame_samples: int, tail_ms: int) -> AecStage:
    """Build an AEC stage, degrading to :class:`NullAec` on any failure.

    Failing soft is deliberate: a missing echo canceller should cost duplex
    quality, not the ability to capture audio at all.
    """
    if mode == "off":
        return NullAec()
    if mode == "speex":
        try:
            return SpeexAec(rate, frame_samples, tail_ms)
        except Exception as exc:
            log.warning(
                "AEC 'speex' unavailable (%s); running without echo "
                "cancellation. Install with: pip install speexdsp-python",
                exc,
            )
            return NullAec()
    if mode == "webrtc":
        log.warning(
            "AEC 'webrtc' is not implemented yet; running without echo "
            "cancellation"
        )
        return NullAec()
    return NullAec()


# -----------------------------------------------------------------------------
# Metering
# -----------------------------------------------------------------------------


class Meter:
    """Rolling level statistics, cheap enough to run on every frame.

    Reports both the raw device level (before any processing) and the level
    of what actually leaves the chain, because the interesting failure —
    analogue gain set wrong — shows up in the former while the latter looks
    fine.
    """

    def __init__(self, alpha: float = 0.2) -> None:
        self._alpha = alpha
        self._in_rms = 0.0
        self._out_rms = 0.0
        self._in_peak = 0.0
        self._out_peak = 0.0
        self.frames = 0
        self.clipped = 0

    @staticmethod
    def _rms(x: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0

    def _ema(self, prev: float, value: float) -> float:
        return value if self.frames == 0 else (1 - self._alpha) * prev + self._alpha * value

    def update(self, raw: np.ndarray, out: np.ndarray, clipped: int) -> None:
        self._in_rms = self._ema(self._in_rms, self._rms(raw))
        self._out_rms = self._ema(self._out_rms, self._rms(out))
        self._in_peak = max(self._in_peak * 0.995, float(np.max(np.abs(raw))) if len(raw) else 0.0)
        self._out_peak = max(self._out_peak * 0.995, float(np.max(np.abs(out))) if len(out) else 0.0)
        self.clipped += clipped
        self.frames += 1

    def snapshot(self, dc_offset: float = 0.0) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "input_rms_dbfs": round(dbfs(self._in_rms), 1),
            "input_peak_dbfs": round(dbfs(self._in_peak), 1),
            "output_rms_dbfs": round(dbfs(self._out_rms), 1),
            "output_peak_dbfs": round(dbfs(self._out_peak), 1),
            "dc_offset_lsb": round(dc_offset, 1),
            "clipped_samples": self.clipped,
        }

    def reset(self) -> None:
        self.__init__(self._alpha)  # type: ignore[misc]


# -----------------------------------------------------------------------------
# The chain
# -----------------------------------------------------------------------------


class CaptureChain:
    """Turns raw interleaved device bytes into mono stream-rate PCM.

    One instance per capture session. Not thread-safe: call :meth:`process`
    from one context only. :meth:`retune` is safe to call between frames from
    the same context.
    """

    def __init__(
        self,
        config: CaptureConfig,
        reference: ReferenceTap | None = None,
    ) -> None:
        self.config = config
        self.reference = reference
        self.meter = Meter()
        self._dc: DcBlocker | None = None
        self._decimator: FirDecimator | None = None
        self._hpf: HighPass | None = None
        self._aec: AecStage = NullAec()
        self._build(config, rebuild_all=True)

    # ---- Construction ----

    def _build(self, config: CaptureConfig, *, rebuild_all: bool, old: CaptureConfig | None = None) -> None:
        changed = (lambda *names: True) if rebuild_all else (
            lambda *names: any(getattr(config, n) != getattr(old, n) for n in names)
        )

        if changed("dc_block"):
            self._dc = DcBlocker() if config.dc_block else None
        if changed("stream_rate", "device_rate"):
            factor = config.decimation
            self._decimator = FirDecimator(factor) if factor > 1 else None
        if changed("hpf_hz", "hpf_mode", "stream_rate"):
            self._hpf = make_highpass(config.stream_rate, config.hpf_hz, config.hpf_mode)
        if changed("aec", "aec_tail_ms", "stream_rate", "frame_ms"):
            try:
                self._aec.close()
            except Exception:
                log.exception("AEC close failed during rebuild")
            self._aec = make_aec(
                config.aec,
                config.stream_rate,
                config.output_frame_samples,
                config.aec_tail_ms,
            )
        self._gain = float(10.0 ** (config.gain_db / 20.0))
        self._aec_delay = int(config.stream_rate * config.aec_delay_ms / 1000)

    def retune(self, **changes: Any) -> CaptureConfig:
        """Apply parameter changes live, rebuilding only what they affect.

        ``device_rate``, ``device_channels`` and ``frame_ms`` are rejected —
        those require reopening the device, which is the backend's job.

        Returns the new configuration. Raises ValueError on an invalid
        combination, leaving the existing configuration untouched.
        """
        fixed = {"device_rate", "device_channels", "frame_ms"}
        offending = fixed & set(changes)
        if offending:
            raise ValueError(
                f"cannot retune {sorted(offending)} live — these need the "
                f"capture device reopened; restart capture instead"
            )
        unknown = set(changes) - set(CaptureConfig.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown capture parameters: {sorted(unknown)}")

        old = self.config
        new = replace(old, **changes)  # __post_init__ validates
        self._build(new, rebuild_all=False, old=old)
        self.config = new
        log.info("capture chain retuned: %s", changes)
        return new

    @property
    def latency_ms(self) -> float:
        """Group delay the chain adds, in milliseconds at the stream rate."""
        samples = 0
        if self._decimator is not None:
            samples += self._decimator.latency_samples // self._decimator.factor
        if self._hpf is not None:
            samples += self._hpf.latency_samples
        return 1000.0 * samples / self.config.stream_rate

    # ---- Runtime ----

    def process(self, raw: bytes) -> bytes:
        """One device period in, one stream-rate mono frame out (s16le)."""
        cfg = self.config
        samples = np.frombuffer(raw, dtype="<i2")
        if cfg.device_channels > 1:
            usable = (len(samples) // cfg.device_channels) * cfg.device_channels
            frames = samples[:usable].reshape(-1, cfg.device_channels)
            left = frames[:, 0].astype(np.float32)
            right = frames[:, 1].astype(np.float32)
            if cfg.channel_mode == "left":
                x = left
            elif cfg.channel_mode == "right":
                x = right
            elif cfg.channel_mode == "sum":
                x = (left + right) * 0.5
            elif cfg.channel_mode == "diff":
                x = (left - right) * 0.5
            else:  # "mono" — average, same as sum but named for intent
                x = (left + right) * 0.5
        else:
            x = samples.astype(np.float32)

        raw_view = x
        if self._dc is not None:
            x = self._dc.process(x)
        if self._decimator is not None:
            x = self._decimator.process(x)
        if self._hpf is not None:
            x = self._hpf.process(x)
        if not isinstance(self._aec, NullAec) and self.reference is not None:
            far = self.reference.take(len(x), self._aec_delay)
            x = self._aec.process(x, far)
        if self._gain != 1.0:
            x = x * self._gain

        clipped = int(np.count_nonzero(np.abs(x) >= 32767.0))
        out = np.clip(x, -32768.0, 32767.0).astype("<i2")
        self.meter.update(raw_view, x, clipped)
        return out.tobytes()

    def stats(self) -> dict[str, Any]:
        """Level statistics plus the effective configuration."""
        snap = self.meter.snapshot(self._dc.offset if self._dc else 0.0)
        snap["latency_ms"] = round(self.latency_ms, 2)
        snap["aec"] = self._aec.name
        snap["config"] = self.config.as_dict()
        return snap

    def reset_stats(self) -> None:
        self.meter.reset()

    def close(self) -> None:
        try:
            self._aec.close()
        except Exception:
            log.exception("AEC close failed")
        self._aec = NullAec()


# -----------------------------------------------------------------------------
# Playback-side helper
# -----------------------------------------------------------------------------


def to_reference(
    pcm: bytes,
    *,
    samplerate: int,
    channels: int,
    target_rate: int,
) -> np.ndarray | None:
    """Fold a playback chunk down to mono at the AEC rate.

    Returns None when the rate is not an integer multiple of the target, since
    a proper resampler is out of scope for the scaffold — the caller should
    log once and feed the tap nothing rather than feed it something wrong.
    """
    if samplerate % target_rate != 0:
        return None
    samples = np.frombuffer(pcm, dtype="<i2")
    if channels > 1:
        usable = (len(samples) // channels) * channels
        mono = samples[:usable].reshape(-1, channels).mean(axis=1).astype(np.float32)
    else:
        mono = samples.astype(np.float32)
    factor = samplerate // target_rate
    if factor > 1:
        # Crude but adequate for a reference signal: block-average rather than
        # a filtered decimation. The canceller adapts around the difference.
        usable = (len(mono) // factor) * factor
        mono = mono[:usable].reshape(-1, factor).mean(axis=1)
    return mono.astype(np.float32)
