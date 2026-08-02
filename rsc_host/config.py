"""Runtime configuration for the host service.

Env-var driven; sensible defaults for laptop development. Production deployment
(systemd unit on the Pi) sets these explicitly.

Variables:

    RSC_HOST_BIND               Interface to bind (default: 127.0.0.1)
    RSC_HOST_PORT               TCP port (default: 8765)
    RSC_HOST_TOKEN              Bearer token — REQUIRED, no default in prod
    RSC_HOST_BACKEND            HAL backend: fake | pi (default: fake)
    RSC_HOST_TLS_CERT           PEM cert path; enables TLS if set
    RSC_HOST_TLS_KEY            PEM key path; enables TLS if set
    RSC_HOST_LOG_LEVEL          Python log level (default: INFO)
    RSC_HOST_ADVERTISE          LAN mDNS discovery: true | false (default: true)
    RSC_HOST_ROBOT_NAME         Override the advertised name (default: hostname)
    RSC_HOST_AUDIO_INPUT        ALSA input device — int index or name string
                                (default: ALSA default)
    RSC_HOST_AUDIO_OUTPUT       ALSA output device — int index or name string
                                (default: ALSA default)
    RSC_HOST_AUDIO_SAMPLERATE   Capture rate in Hz (default: 16000)
    RSC_HOST_AUDIO_CHANNELS     Capture channels (default: 1)

The token has no default: laptop dev must set ``RSC_HOST_TOKEN=dev``
explicitly. This prevents accidental production runs with a guessable secret.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["fake", "pi"]


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
    audio_input: str | int | None
    audio_output: str | int | None
    audio_samplerate: int
    audio_channels: int

    @property
    def tls_enabled(self) -> bool:
        """TLS is enabled iff *both* cert and key paths are set."""
        return self.tls_cert is not None and self.tls_key is not None


def load_from_env() -> Config:
    """Read configuration from environment variables.

    Raises:
        RuntimeError: if RSC_HOST_TOKEN is unset or empty.
        ValueError:   if RSC_HOST_BACKEND is not 'fake' or 'pi'.
        ValueError:   if only one of RSC_HOST_TLS_CERT / _TLS_KEY is set.
    """
    token = os.environ.get("RSC_HOST_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "RSC_HOST_TOKEN is not set. Refusing to start without an auth token. "
            "For laptop dev, set RSC_HOST_TOKEN=dev explicitly."
        )

    backend_raw = os.environ.get("RSC_HOST_BACKEND", "fake").strip().lower()
    if backend_raw not in ("fake", "pi"):
        raise ValueError(
            f"RSC_HOST_BACKEND must be 'fake' or 'pi', got {backend_raw!r}"
        )

    tls_cert = os.environ.get("RSC_HOST_TLS_CERT", "").strip() or None
    tls_key = os.environ.get("RSC_HOST_TLS_KEY", "").strip() or None
    if bool(tls_cert) != bool(tls_key):
        raise ValueError(
            "RSC_HOST_TLS_CERT and RSC_HOST_TLS_KEY must be set together, "
            "or both unset."
        )

    advertise_raw = os.environ.get("RSC_HOST_ADVERTISE", "true").strip().lower()
    if advertise_raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(
            f"RSC_HOST_ADVERTISE must be true/false, got {advertise_raw!r}"
        )
    advertise = advertise_raw in ("true", "1", "yes")

    robot_name = os.environ.get("RSC_HOST_ROBOT_NAME", "").strip() or None

    # Audio device: accept either an integer index (0, 1, ...) or a name
    # substring (e.g. "USB Microphone", "seeed"). An empty value means
    # "use ALSA default".
    def _parse_device(varname: str) -> str | int | None:
        raw = os.environ.get(varname, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return raw

    audio_input = _parse_device("RSC_HOST_AUDIO_INPUT")
    audio_output = _parse_device("RSC_HOST_AUDIO_OUTPUT")

    try:
        audio_samplerate = int(os.environ.get("RSC_HOST_AUDIO_SAMPLERATE", "16000"))
    except ValueError as exc:
        raise ValueError(
            f"RSC_HOST_AUDIO_SAMPLERATE must be an integer: {exc}"
        ) from exc
    if audio_samplerate <= 0:
        raise ValueError(
            f"RSC_HOST_AUDIO_SAMPLERATE must be > 0, got {audio_samplerate}"
        )

    try:
        audio_channels = int(os.environ.get("RSC_HOST_AUDIO_CHANNELS", "1"))
    except ValueError as exc:
        raise ValueError(
            f"RSC_HOST_AUDIO_CHANNELS must be an integer: {exc}"
        ) from exc
    if audio_channels not in (1, 2):
        raise ValueError(
            f"RSC_HOST_AUDIO_CHANNELS must be 1 or 2, got {audio_channels}"
        )

    return Config(
        bind=os.environ.get("RSC_HOST_BIND", "127.0.0.1").strip(),
        port=int(os.environ.get("RSC_HOST_PORT", "8765")),
        token=token,
        backend=backend_raw,  # type: ignore[arg-type]  # narrowed by the check above
        tls_cert=tls_cert,
        tls_key=tls_key,
        log_level=os.environ.get("RSC_HOST_LOG_LEVEL", "INFO").strip().upper(),
        advertise=advertise,
        robot_name=robot_name,
        audio_input=audio_input,
        audio_output=audio_output,
        audio_samplerate=audio_samplerate,
        audio_channels=audio_channels,
    )
