"""
tests/respeaker.py — ReSpeaker 2-Mic HAT subtests.

Subtests
--------
  record      record 3 s from both mics, play back immediately
  leds        cycle through colours on the 3× APA102 LEDs
  hatbutton   report press/release events from the HAT's user button (GPIO17)
"""

import time

from config import GPIO, ReSpeaker as RSCfg, Audio


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Check ALSA capture device and HAT button GPIO are accessible."""
    issues = []

    # ALSA capture
    import subprocess
    r = subprocess.run(
        ["arecord", "-D", Audio.ALSA_CAPTURE, "--duration=0", "-q",
         "-f", Audio.BIT_DEPTH, "-r", str(Audio.SAMPLE_RATE),
         "-c", str(Audio.CHANNELS), "/dev/null"],
        capture_output=True, timeout=3,
    )
    if r.returncode != 0:
        issues.append(f"ALSA capture {Audio.ALSA_CAPTURE} unavailable")

    # HAT button GPIO
    try:
        from gpiozero import Button
        b = Button(GPIO.RESPEAKER_BTN, pull_up=False)
        b.close()
    except Exception as e:
        issues.append(f"HAT button GPIO{GPIO.RESPEAKER_BTN}: {e}")

    if issues:
        return False, "; ".join(issues)
    return True, f"ALSA capture OK · HAT button GPIO{GPIO.RESPEAKER_BTN} accessible"


# ── Subtests ──────────────────────────────────────────────────────────────────

def _record():
    """Record Audio.RECORD_SECS seconds then play back."""
    import subprocess
    wav = Audio.TMP_WAV
    print(f"recording {Audio.RECORD_SECS} s from {Audio.ALSA_CAPTURE}...")
    subprocess.run([
        "arecord",
        "-D", Audio.ALSA_CAPTURE,
        "-f", Audio.BIT_DEPTH,
        "-r", str(Audio.SAMPLE_RATE),
        "-c", str(Audio.CHANNELS),
        "-d", str(Audio.RECORD_SECS),
        wav,
    ], check=True)
    print(f"playing back via {Audio.ALSA_PLAYBACK}...")
    subprocess.run(["aplay", "-D", Audio.ALSA_PLAYBACK, wav], check=True)
    print("done.")


def _leds():
    """Cycle through red, green, blue, white on the 3× APA102 LEDs then clear."""
    try:
        import apa102
    except ImportError:
        print("apa102 not installed — pip install apa102-pi")
        return

    NUM = RSCfg.LED_COUNT
    dev = apa102.APA102(num_led=NUM)

    colours = [
        (180,   0,   0, "red"),
        (  0, 180,   0, "green"),
        (  0,   0, 180, "blue"),
        (180, 180, 180, "white"),
    ]

    for r, g, b, name in colours:
        print(f"LEDs → {name}")
        for i in range(NUM):
            dev.set_pixel(i, r, g, b)
        dev.show()
        time.sleep(0.8)

    dev.clear_strip()
    dev.cleanup()
    print("LEDs cleared.")


def _hatbutton():
    """Report press/release on the HAT's GPIO17 button. ctrl-c to stop."""
    from gpiozero import Button
    from signal import pause

    btn = Button(GPIO.RESPEAKER_BTN, pull_up=False)
    btn.when_pressed  = lambda: print("HAT button pressed")
    btn.when_released = lambda: print("HAT button released")

    print(f"watching HAT button GPIO{GPIO.RESPEAKER_BTN} — ctrl-c to stop")
    try:
        pause()
    except KeyboardInterrupt:
        pass
    finally:
        btn.close()


_SUBTESTS = {
    "record":    _record,
    "leds":      _leds,
    "hatbutton": _hatbutton,
}


# ── Run ───────────────────────────────────────────────────────────────────────

def run(subtest="record"):
    _SUBTESTS[subtest]()
