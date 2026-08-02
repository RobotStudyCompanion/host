"""``rsc-host-audio-check`` — enumerate audio devices from the shell.

Runs standalone (no daemon needed). Prints the same shape that
``audio.devices`` returns over the WebSocket, so users can pick device
indices or names for :envvar:`RSC_HOST_AUDIO_INPUT` /
:envvar:`RSC_HOST_AUDIO_OUTPUT` without needing a client.

Usage::

    rsc-host-audio-check

Also lets you smoke-test a specific device::

    rsc-host-audio-check --test-play  1          # 1-sec beep on output device 1
    rsc-host-audio-check --test-record 0 --sec 3 # 3-sec capture from input device 0
"""
from __future__ import annotations

import argparse
import sys
from typing import Any


def _print_devices() -> None:
    try:
        import sounddevice as sd
    except ImportError:
        print(
            "sounddevice not installed. Install with: pip install sounddevice",
            file=sys.stderr,
        )
        sys.exit(2)

    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    print(f"Default input:  {default_in}")
    print(f"Default output: {default_out}")
    print()
    print(f"{'idx':<4} {'in':<4} {'out':<4} {'rate':<8} name")
    print("-" * 60)
    for i, d in enumerate(devices):
        marker_in = "*" if i == default_in else ""
        marker_out = "*" if i == default_out else ""
        print(
            f"{i:<4} "
            f"{str(d['max_input_channels']) + marker_in:<4} "
            f"{str(d['max_output_channels']) + marker_out:<4} "
            f"{int(d['default_samplerate']):<8} "
            f"{d['name']}"
        )


def _test_play(device: str | int, freq: float = 440.0, sec: float = 1.0) -> None:
    import numpy as np
    import sounddevice as sd

    sr = 48000
    t = np.linspace(0, sec, int(sr * sec), endpoint=False)
    audio = 0.3 * np.sin(2 * np.pi * freq * t).astype("float32")
    print(f"Playing {freq} Hz for {sec}s on device {device!r}...")
    sd.play(audio, samplerate=sr, device=device)
    sd.wait()
    print("done")


def _test_record(device: str | int, sec: float = 3.0, samplerate: int = 16000) -> None:
    import sounddevice as sd
    import numpy as np

    print(f"Recording {sec}s from device {device!r} at {samplerate} Hz...")
    frames = sd.rec(
        int(sec * samplerate),
        samplerate=samplerate,
        channels=1,
        dtype="int16",
        device=device,
    )
    sd.wait()
    # Basic sanity: peak level.
    peak = int(np.abs(frames).max())
    rms = float(np.sqrt((frames.astype("float32") ** 2).mean()))
    print(f"done — peak={peak}, rms={rms:.1f}")
    print(f"       (peak ≈ 0 means silence; peak > 5000 means signal is landing)")


def _parse_device(raw: str) -> str | int:
    try:
        return int(raw)
    except ValueError:
        return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--test-play",
        metavar="DEVICE",
        help="Play a 440 Hz beep on the given output device (index or name).",
    )
    parser.add_argument(
        "--test-record",
        metavar="DEVICE",
        help="Record N seconds from the given input device (index or name).",
    )
    parser.add_argument(
        "--sec",
        type=float,
        default=3.0,
        help="Duration for --test-record (default: 3.0).",
    )
    parser.add_argument(
        "--samplerate",
        type=int,
        default=16000,
        help="Sample rate for --test-record (default: 16000).",
    )
    args = parser.parse_args()

    if args.test_play is None and args.test_record is None:
        _print_devices()
        return 0

    if args.test_play is not None:
        _test_play(_parse_device(args.test_play))
    if args.test_record is not None:
        _test_record(_parse_device(args.test_record), sec=args.sec, samplerate=args.samplerate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
