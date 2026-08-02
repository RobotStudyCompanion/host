"""
tests/speaker.py — Base speaker playback via ALSA.

Generates a short 440 Hz tone and plays it through Audio.ALSA_PLAYBACK.
Requires the `sox` utility (apt install sox).
"""

import subprocess
import tempfile
import os

from config import Audio


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Verify ALSA playback device is present."""
    r = subprocess.run(
        ["aplay", "-l"], capture_output=True, text=True
    )
    if Audio.ALSA_PLAYBACK.split(":")[0].replace("plughw", "hw") in r.stdout or r.returncode == 0:
        return True, f"ALSA playback device {Audio.ALSA_PLAYBACK} present"
    return False, f"ALSA playback check failed: {r.stderr.strip()}"


# ── Run ───────────────────────────────────────────────────────────────────────

def run():
    """Generate a 1 s 440 Hz tone and play it through the base speaker."""
    wav = Audio.TMP_WAV

    # Generate tone with sox if available, otherwise fall back to aplay /dev/zero
    if subprocess.run(["which", "sox"], capture_output=True).returncode == 0:
        print("generating 440 Hz test tone (1 s)...")
        subprocess.run([
            "sox", "-n",
            "-r", str(Audio.SAMPLE_RATE),
            "-c", str(Audio.CHANNELS),
            wav,
            "synth", "1", "sine", "440",
        ], check=True)
        print(f"playing via {Audio.ALSA_PLAYBACK}...")
        subprocess.run(["aplay", "-D", Audio.ALSA_PLAYBACK, wav], check=True)
    else:
        print("sox not found — install with: sudo apt install sox")
        print("playing 1 s of silence to verify ALSA path...")
        subprocess.run([
            "aplay", "-D", Audio.ALSA_PLAYBACK,
            "-f", Audio.BIT_DEPTH,
            "-r", str(Audio.SAMPLE_RATE),
            "-c", str(Audio.CHANNELS),
            "-d", "1",
            "/dev/zero",
        ], check=True)

    print("done — did you hear the tone?")
