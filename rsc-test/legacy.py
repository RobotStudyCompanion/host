#!/usr/bin/env python3
"""Button + LED + ring + servo test rig for the RSC front panel.

BUTTON  (GPIO23) idles low, goes high on press.
LED_PWM (GPIO24) drives the arcade button's LED through Q1.
RING    (GPIO12) drives 16x SKC6812 RGBW NeoPixel ring via J6.
SERVOS  (GPIO13/26) M2_PWM (J7/left), M3_PWM (J8/right).

Privileges
----------
Button, LED and servos run as an unprivileged user provided that user is in
the `gpio` group (check with `id -nG`). Ring behaviours ("ring", "sweep")
require root, because the NeoPixel driver maps /dev/mem to reach the PWM
peripheral and DMA controller directly. Run those with sudo:

    sudo /home/rsc/rsc-env/bin/python rsc_test.py ring
"""

import argparse
import atexit
import os
import shutil
import subprocess
import tempfile
import threading
import time

import lgpio
from gpiozero import Button, PWMLED
from signal import pause

BUTTON_PIN  = 23
LED_PIN     = 24
BOUNCE_TIME = 0.05
HOLD_TIME   = 1
RING_COUNT  = 16          # SKC6812 RGBW 16-pixel ring

SERVO_PINS  = [13, 26]    # M2_PWM (J7/left), M3_PWM (J8/right)
SERVO_STOP  = 1500        # us - neutral

SERVO_R_TRIM = 105        # us - increase to speed up right, decrease to slow it

SERVO_L_FWD = 1600        # us - left forward
SERVO_L_REV = 1400        # us - left reverse
SERVO_R_FWD = 1400 - SERVO_R_TRIM   # = 1295
SERVO_R_REV = 1600 + SERVO_R_TRIM   # = 1705

# WM8960 on the ReSpeaker 2-Mic HAT. Native rate is 48k - resampling to 16k
# inside plughw on this driver produces audible warble, so capture at 48k and
# resample downstream if a model needs it.
ALSA_DEVICE = "plughw:0,0"
ALSA_RATE   = 48000
ALSA_FORMAT = "S16_LE"
ALSA_CHANS  = 2
MIC_SECONDS = 5


# --- Behaviour base -------------------------------------------------------

class Behaviour:
    """Base class for a button/LED behaviour - override what you need."""

    def __init__(self, button, led, ring=None):
        self.button = button
        self.led    = led
        self.ring   = ring

    def on_press(self):   pass
    def on_release(self): pass
    def on_hold(self):    pass

    def attach(self):
        self.button.when_pressed  = self.on_press
        self.button.when_released = self.on_release
        self.button.when_held     = self.on_hold


class PrintOnly(Behaviour):
    """Just log events - the minimal button sanity check."""

    def on_press(self):   print("pressed")
    def on_release(self): print("released")


class SolidWhileHeld(Behaviour):
    """Breathes when idle, goes solid while the button is down."""

    def attach(self):
        super().attach()
        self.led.pulse()

    def on_press(self):
        print("pressed")
        self.led.on()

    def on_release(self):
        print("released")
        self.led.pulse()


class BrightnessCycle(Behaviour):
    """Each press steps through a few brightness levels."""

    levels = [0.0, 0.25, 0.5, 0.75, 1.0]

    def __init__(self, button, led, ring=None):
        super().__init__(button, led, ring)
        self.index = 0

    def on_press(self):
        self.index = (self.index + 1) % len(self.levels)
        self.led.value = self.levels[self.index]
        print(f"brightness -> {self.levels[self.index]:.0%}")


class TapVsHold(Behaviour):
    """Distinguishes a quick tap from a long press."""

    def on_press(self):   print("pressed")
    def on_release(self): print("released")
    def on_hold(self):    print("held")


class Poll(Behaviour):
    """Polls the button line directly, bypassing edge callbacks entirely.

    Discriminator test: if this prints changing values but the edge-driven
    behaviours stay silent, the line is being sampled and the fault sits in
    the callback layer, not in the pin claim.
    """

    def attach(self):
        # No gpiozero Button exists for this behaviour - main() skips it, so
        # this claim is the only holder of the line.
        #
        # SET_PULL_DOWN matters: claiming with no pull leaves the pin floating,
        # so the first press drives it high and pin capacitance holds it there
        # forever. The button idles low, so a pull-down is what restores it.
        chip = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_input(chip, BUTTON_PIN, lgpio.SET_PULL_DOWN)
        print(f"polling GPIO{BUTTON_PIN} - press the button, ctrl-c to stop")
        presses = 0
        try:
            last = None
            while True:
                val = lgpio.gpio_read(chip, BUTTON_PIN)
                if val != last:
                    if val == 1:
                        presses += 1
                        print(f"  level -> 1   (press {presses})")
                    elif last is not None:
                        print(f"  level -> 0")
                    else:
                        print(f"  level -> 0   (idle)")
                    last = val
                time.sleep(0.02)
        except KeyboardInterrupt:
            print(f"\nstopping - {presses} presses seen")
        finally:
            lgpio.gpio_free(chip, BUTTON_PIN)
            lgpio.gpiochip_close(chip)


# --- Ring behaviours ------------------------------------------------------

class RingColour(Behaviour):
    """Ring fills on press, clears on release. LED breathes throughout."""

    PRESS_COLOUR   = (0, 80, 255, 0)   # RGBW - blue, white channel off
    RELEASE_COLOUR = (0, 0, 0, 0)

    def attach(self):
        super().attach()
        self.led.pulse()
        if self.ring:
            self.ring.fill(self.RELEASE_COLOUR)
            self.ring.show()

    def on_press(self):
        print("pressed")
        if self.ring:
            self.ring.fill(self.PRESS_COLOUR)
            self.ring.show()

    def on_release(self):
        print("released")
        if self.ring:
            self.ring.fill(self.RELEASE_COLOUR)
            self.ring.show()


class RingSweep(Behaviour):
    """Sweeps a single pixel around the ring while held, clears on release."""

    COLOUR = (0, 80, 255, 0)   # RGBW - blue, white channel off

    def __init__(self, button, led, ring=None):
        super().__init__(button, led, ring)
        self._running = False
        self._thread  = None

    def attach(self):
        super().attach()
        self.led.pulse()
        if self.ring:
            self.ring.fill((0, 0, 0, 0))
            self.ring.show()

    def on_press(self):
        print("pressed - sweeping")
        if not self.ring or self._running:
            return
        self._running = True
        self.ring.fill((0, 0, 0, 0))
        self.ring.show()

        def sweep():
            prev = 0
            while self._running:
                for i in range(len(self.ring)):
                    if not self._running:
                        break
                    self.ring[prev] = (0, 0, 0, 0)
                    self.ring[i]    = self.COLOUR
                    self.ring.show()
                    prev = i
                    time.sleep(0.06)

        self._thread = threading.Thread(target=sweep, daemon=True)
        self._thread.start()

    def on_release(self):
        print("released")
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.ring:
            self.ring.fill((0, 0, 0, 0))
            self.ring.show()


# --- Servo behaviours -----------------------------------------------------

class ServoBase(Behaviour):
    """Shared servo helpers. `chip` is an open lgpio chip handle, or None."""

    def __init__(self, button, led, ring=None, chip=None):
        super().__init__(button, led, ring)
        self.chip = chip

    def _set(self, l_pw, r_pw):
        if self.chip is None:
            return
        lgpio.tx_servo(self.chip, SERVO_PINS[0], l_pw)
        lgpio.tx_servo(self.chip, SERVO_PINS[1], r_pw)

    def _stop(self):
        """Neutral briefly, then cease pulses altogether."""
        if self.chip is None:
            return
        self._set(SERVO_STOP, SERVO_STOP)
        time.sleep(0.1)
        for pin in SERVO_PINS:
            lgpio.tx_servo(self.chip, pin, 0)


class ServoHold(ServoBase):
    """Both servos forward while held, stop on release. LED breathes."""

    def attach(self):
        super().attach()
        self.led.pulse()

    def on_press(self):
        print("servo - forward")
        self._set(SERVO_L_FWD, SERVO_R_FWD)

    def on_release(self):
        print("servo - stop")
        self._stop()


class ServoCycle(ServoBase):
    """Tap cycles both servos: forward -> reverse -> stop -> repeat."""

    STATES = [
        (SERVO_L_FWD, SERVO_R_FWD, "forward"),
        (SERVO_L_REV, SERVO_R_REV, "reverse"),
        (SERVO_STOP,  SERVO_STOP,  "stop"),
    ]

    def __init__(self, button, led, ring=None, chip=None):
        super().__init__(button, led, ring, chip)
        self.index = 0

    def attach(self):
        super().attach()
        self.led.pulse()
        self._stop()

    def on_press(self):
        l_pw, r_pw, label = self.STATES[self.index]
        print(f"servo - {label}")
        if label == "stop":
            self._stop()
        else:
            self._set(l_pw, r_pw)
        self.index = (self.index + 1) % len(self.STATES)


class ServoHoldDir(ServoBase):
    """Press -> forward, hold -> reverse, release -> stop. LED breathes."""

    def attach(self):
        super().attach()
        self.led.pulse()

    def on_press(self):
        print("servo - forward")
        self._set(SERVO_L_FWD, SERVO_R_FWD)

    def on_hold(self):
        print("servo - reverse")
        self._set(SERVO_L_REV, SERVO_R_REV)

    def on_release(self):
        print("servo - stop")
        self._stop()


class ServoSweep(ServoBase):
    """Open-loop sweep with no button involvement - pure output test.

    Steps each servo through its range on a timer so you can confirm motion
    without depending on edge detection working.
    """

    def attach(self):
        self.led.pulse()
        if self.chip is None:
            print("no chip handle - nothing to drive")
            return
        print("sweeping both servos - ctrl-c to stop")
        try:
            while True:
                for us in (1000, 1500, 2000, 1500):
                    print(f"  pulse -> {us}us")
                    for pin in SERVO_PINS:
                        lgpio.tx_servo(self.chip, pin, us)
                    time.sleep(1.5)
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            self._stop()


# --- Mixed integration ----------------------------------------------------

def _make_tone(path, freq=660, secs=0.35, amplitude=0.4):
    """Write a short sine-tone wav. Pure stdlib - no numpy needed."""
    import math
    import struct
    import wave

    frames = int(ALSA_RATE * secs)
    fade   = int(ALSA_RATE * 0.01)   # 10ms fades to avoid clicks
    with wave.open(path, "w") as w:
        w.setnchannels(ALSA_CHANS)
        w.setsampwidth(2)
        w.setframerate(ALSA_RATE)
        data = bytearray()
        for n in range(frames):
            env = 1.0
            if n < fade:
                env = n / fade
            elif n > frames - fade:
                env = (frames - n) / fade
            val = int(32767 * amplitude * env * math.sin(2 * math.pi * freq * n / ALSA_RATE))
            data += struct.pack("<h", val) * ALSA_CHANS
        w.writeframes(bytes(data))
    return path


class Mixed(ServoBase):
    """Everything at once - the contention test.

    Press drives the ring sweeping, both servos forward, the LED solid and a
    tone through the speaker simultaneously. If ring DMA, lgpio software servo
    timing and I2S audio interfere with each other, this is where it shows.
    """

    COLOUR = (0, 80, 255, 0)

    def __init__(self, button, led, ring=None, chip=None):
        super().__init__(button, led, ring, chip)
        self._running = False
        self._thread  = None
        self._tone    = None
        try:
            self._tone = _make_tone(
                os.path.join(tempfile.gettempdir(), "rsc_mixed_tone.wav")
            )
        except Exception as e:
            print(f"tone unavailable - {e}")

    def attach(self):
        super().attach()
        self.led.pulse()
        if self.ring:
            self.ring.fill((0, 0, 0, 0))
            self.ring.show()
        have = []
        have.append("ring" if self.ring else "ring:NO")
        have.append("servo" if self.chip is not None else "servo:NO")
        have.append("audio" if self._tone else "audio:NO")
        print("mixed mode - " + ", ".join(have))

    def _play(self):
        if not self._tone or shutil.which("aplay") is None:
            return
        try:
            subprocess.Popen(
                ["aplay", "-q", "-D", ALSA_DEVICE, self._tone],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def on_press(self):
        print("press - ring + servos + LED + tone")
        self.led.on()
        self._set(SERVO_L_FWD, SERVO_R_FWD)
        self._play()
        if self.ring and not self._running:
            self._running = True
            self.ring.fill((0, 0, 0, 0))
            self.ring.show()

            def sweep():
                prev = 0
                while self._running:
                    for i in range(len(self.ring)):
                        if not self._running:
                            break
                        self.ring[prev] = (0, 0, 0, 0)
                        self.ring[i]    = self.COLOUR
                        self.ring.show()
                        prev = i
                        time.sleep(0.06)

            self._thread = threading.Thread(target=sweep, daemon=True)
            self._thread.start()

    def on_release(self):
        print("release - all stop")
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._stop()
        self.led.pulse()
        if self.ring:
            self.ring.fill((0, 0, 0, 0))
            self.ring.show()


# --- Audio ----------------------------------------------------------------
def run_speaker():
    """Play a test tone through the base speaker via the WM8960."""
    if not _require("speaker-test"):
        return
    print(f"playing test tone on {ALSA_DEVICE} - ctrl-c to stop")
    try:
        subprocess.run(
            ["speaker-test", "-D", ALSA_DEVICE,
             "-c", str(ALSA_CHANS), "-t", "wav", "-l", "1"],
            check=False,
        )
    except KeyboardInterrupt:
        print("\nstopping")


def run_mic():
    """Record from the ReSpeaker mics, then play the result straight back."""
    if not (_require("arecord") and _require("aplay")):
        return
    path = os.path.join(tempfile.gettempdir(), "rsc_mic_test.wav")
    print(f"recording {MIC_SECONDS}s from {ALSA_DEVICE} - speak now")
    rec = subprocess.run(
        ["arecord", "-D", ALSA_DEVICE,
         "-f", ALSA_FORMAT, "-r", str(ALSA_RATE), "-c", str(ALSA_CHANS),
         "-d", str(MIC_SECONDS), path],
        check=False,
    )
    if rec.returncode != 0:
        print("capture failed - check `arecord -l` and mixer state")
        return
    print("playing back")
    subprocess.run(["aplay", "-D", ALSA_DEVICE, path], check=False)
    print(f"file kept at {path}")


def run_meter():
    """Live VU meters for setting capture gain without guessing.

    Aim for peaks around 80% on the loudest thing you will ever say. Adjust
    with: amixer -c 0 sset 'Capture' <0-63>
    """
    if not _require("arecord"):
        return
    print("VU meter - speak normally, ctrl-c to stop")
    try:
        subprocess.run(
            ["arecord", "-D", ALSA_DEVICE,
             "-f", ALSA_FORMAT, "-r", str(ALSA_RATE), "-c", str(ALSA_CHANS),
             "-V", "stereo", os.devnull],
            check=False,
        )
    except KeyboardInterrupt:
        print("\nstopping")




def run_gain():
    """Apply the known-good WM8960 mixer state, then offer to persist it.

    Gain staging rationale: put the gain in the analogue boost stage ahead of
    the ADC, not in the digital Capture control behind it. Boost improves the
    signal relative to the noise floor; Capture amplifies both equally. ALC
    stays off - with max gain 7 it pumps hard on a quiet room and produces the
    warble that sounds like distortion.
    """
    if not _require("amixer"):
        return
    settings = [
        ("ALC Function", "Off"),
        ("ADC High Pass Filter", "on"),
        ("Left Boost Mixer LINPUT1", "on"),
        ("Right Boost Mixer RINPUT1", "on"),
        ("Left Input Boost Mixer LINPUT1", "3"),    # +29dB analogue
        ("Right Input Boost Mixer RINPUT1", "3"),
        ("Capture", "35"),                          # +9dB digital
        ("Playback", "255"),                        # 0dB, DAC full scale
        ("Speaker", "121"),                         # 0dB
        ("Headphone", "121"),
        ("Left Output Mixer PCM", "on"),
        ("Right Output Mixer PCM", "on"),
        ("DAC Mono Mix", "Mono"),                   # JST speaker is mono
    ]
    for name, value in settings:
        r = subprocess.run(
            ["amixer", "-c", "0", "sset", name, value],
            check=False, capture_output=True, text=True,
        )
        status = "ok" if r.returncode == 0 else "skip"
        print(f"  {status:<5} {name} -> {value}")
    print("\nnow run `meter` and aim for peaks near 80% at your loudest.")
    print("too hot: amixer -c 0 sset 'Capture' 28")
    print("too low: amixer -c 0 sset 'Capture' 45")
    print("persist: sudo alsactl store -f /var/lib/alsa/asound.state")

# --- Audio analysis primitives -------------------------------------------

def _dbfs(v):
    import math
    return 20 * math.log10(v / 32768) if v > 0 else -120.0


def _rms(xs):
    import math
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def _onepole_lp(xs, fc, rate, poles=3):
    """Cascaded one-pole low-pass. 3 poles = 18dB/octave, enough to keep
    band leakage below the levels we are trying to measure."""
    import math
    a = math.exp(-2 * math.pi * fc / rate)
    b = 1.0 - a
    out = xs
    for _ in range(poles):
        y, nxt = 0.0, []
        for x in out:
            y = b * x + a * y
            nxt.append(y)
        out = nxt
    return out


def _demean(xs):
    """Strip DC offset. Returns (centred samples, offset in LSB)."""
    m = sum(xs) / len(xs) if xs else 0.0
    return [x - m for x in xs], m


def _goertzel(xs, f, rate):
    """Magnitude at a single frequency. Cheaper than an FFT for a few bins."""
    import math
    k = 2 * math.cos(2 * math.pi * f / rate)
    s1 = s2 = 0.0
    for x in xs:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0.0)) / len(xs)


def _capture(path, secs, prompt=None):
    """Record to path. Returns the left channel as a list, or None."""
    import array, wave
    if prompt:
        print(prompt)
    r = subprocess.run(
        ["arecord", "-D", ALSA_DEVICE, "-f", ALSA_FORMAT,
         "-r", str(ALSA_RATE), "-c", str(ALSA_CHANS), "-d", str(secs), path],
        check=False, capture_output=True,
    )
    if r.returncode != 0:
        print("  capture failed - check `arecord -l` and mixer state")
        return None
    with wave.open(path, "rb") as w:
        rate, chans = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    s = array.array("h")
    s.frombytes(raw)
    left = list(s[0::chans] if chans > 1 else s)
    return left[int(rate * 0.25):]          # drop ADC settling transient


BANDS = [
    ("dc/drift  <20Hz", None,  20),
    ("rumble 20-100Hz",   20, 100),
    ("low   100-300Hz",  100, 300),
    ("speech 300-3k4",   300, 3400),
    ("presence 3k4-8k", 3400, 8000),
    ("hiss      >8kHz", 8000, None),
]


def _band_levels(xs, rate):
    """Split into bands by differencing cascaded low-passes."""
    lp = {}
    for fc in (20, 100, 300, 3400, 8000):
        lp[fc] = _onepole_lp(xs, fc, rate)
    out = []
    for name, lo, hi in BANDS:
        if lo is None:
            band = lp[hi]
        elif hi is None:
            band = [a - b for a, b in zip(xs, lp[lo])]
        else:
            band = [a - b for a, b in zip(lp[hi], lp[lo])]
        out.append((name, _dbfs(_rms(band))))
    return out


def _bar(db, floor=-90, ceil=0, width=40):
    frac = max(0.0, min(1.0, (db - floor) / (ceil - floor)))
    n = int(frac * width)
    return "#" * n + "." * (width - n)


def _mains_check(xs, rate):
    """Test for a genuine harmonic series, not just low-frequency tilt."""
    dec_rate = rate // 16
    dec = _onepole_lp(xs, 1000, rate)[::16]
    hits = []
    for h in (50, 100, 150, 200):
        mag = _goertzel(dec, h, dec_rate)
        ref = (_goertzel(dec, h - 13, dec_rate)
               + _goertzel(dec, h + 13, dec_rate)) / 2 or 1e-12
        hits.append((h, mag / ref))
    return hits


# --- Audio runners --------------------------------------------------------

def _require(binary):
    if shutil.which(binary) is None:
        print(f"{binary} not found - install alsa-utils")
        return False
    return True


def run_status():
    """Report the full audio stack state in one screen."""
    if not _require("amixer"):
        return
    print("\n=== audio stack status ===\n")

    r = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
    card = next((l for l in r.stdout.splitlines() if l.startswith("card ")),
                "no card found")
    print(f"  card    : {card.strip()}")
    print(f"  device  : {ALSA_DEVICE} @ {ALSA_RATE}Hz {ALSA_FORMAT} x{ALSA_CHANS}")

    def get(name):
        r = subprocess.run(["amixer", "-c", "0", "sget", name],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            head, _, tail = line.strip().partition(":")
            if head in ("Mono", "Front Left", "Item0") and tail.strip():
                return f"{head}: {tail.strip()}"
        return "?"

    print("\n  capture chain (gain before the ADC beats gain after it):")
    for n in ("Left Boost Mixer LINPUT1", "Left Input Boost Mixer LINPUT1",
              "Capture", "ALC Function", "ADC High Pass Filter"):
        print(f"    {n:<34} {get(n)}")

    print("\n  playback chain:")
    for n in ("Playback", "Speaker", "Headphone",
              "Left Output Mixer PCM", "DAC Mono Mix"):
        print(f"    {n:<34} {get(n)}")

    print("\n  persistence:")
    state = "/var/lib/alsa/asound.state"
    print(f"    state file       {'present' if os.path.exists(state) else 'MISSING'}")
    r = subprocess.run(["systemctl", "is-enabled", "alsa-restore"],
                       capture_output=True, text=True)
    st = r.stdout.strip() or "unknown"
    warn = "  <-- masked, settings will not survive reboot" if st == "masked" else ""
    print(f"    alsa-restore     {st}{warn}")

    print("\n  contention:")
    r = subprocess.run(["pgrep", "-a", "pigpiod"], capture_output=True, text=True)
    print(f"    pigpiod          {'RUNNING - will fight for timing' if r.stdout.strip() else 'not running'}")
    print()


def run_noise():
    """Characterise the noise floor by band, with a real harmonic test."""
    if not _require("arecord"):
        return
    path = os.path.join(tempfile.gettempdir(), "rsc_noise.wav")
    xs = _capture(path, 4, "recording 4s - stay SILENT, do not move")
    if not xs:
        return
    xs, dc_offset = _demean(xs)
    print(f"\n  DC offset   : {dc_offset:+8.1f} LSB "
          f"({_dbfs(abs(dc_offset)):.1f} dBFS)")
    if abs(dc_offset) > 300:
        print("    ^ significant. Costs headroom but harmless for STT -")
        print("      every sane pipeline removes DC. Not a hardware fault.")

    total, peak = _rms(xs), max(abs(x) for x in xs)
    print(f"\n  noise floor : {_dbfs(total):6.1f} dBFS rms")
    print(f"  peak        : {_dbfs(peak):6.1f} dBFS")
    print(f"  crest       : {_dbfs(peak) - _dbfs(total):6.1f} dB")

    print("\n  spectrum by band:")
    levels = _band_levels(xs, ALSA_RATE)
    for name, db in levels:
        print(f"    {name}  {db:6.1f} dBFS  {_bar(db)}")

    print("\n  mains harmonic test (>4x means real coupling):")
    for h, ratio in _mains_check(xs, ALSA_RATE):
        flag = "  <-- present" if ratio > 4 else ""
        print(f"    {h:3d} Hz  {ratio:5.1f}x local{flag}")

    by = dict(levels)
    dc     = by["dc/drift  <20Hz"]
    rumble = by["rumble 20-100Hz"]
    speech = by["speech 300-3k4"]
    hiss   = by["hiss      >8kHz"]

    print("\n  assessment:")
    if dc > speech + 12:
        print(f"    Sub-20Hz content {dc - speech:.0f}dB above speech. This is")
        print("    inaudible drift, not sound - the WM8960 high-pass corner is")
        print("    near 4Hz so it passes through. Costs headroom only.")
        print("    Fix in software: `clean` mode, or highpass at 80Hz.")
    if rumble > speech + 12:
        print(f"    Mechanical rumble {rumble - speech:.0f}dB above speech.")
        print("    Sources: fan, chassis resonance, desk coupling, HVAC.")
        print("    Fix physically - foam or sorbothane under the unit - and")
        print("    filter the remainder at 80Hz.")
    if hiss > speech + 6:
        print("    Hiss above the speech band - analogue boost too high.")
    if max(dc, rumble) < speech + 12 and hiss < speech:
        print("    Clean floor. Nothing worth chasing.")
    print(f"\n  file kept at {path}")


def run_tune():
    """Sweep the analogue boost and pick the setting with the best SNR.

    SNR in the speech band is what matters, not absolute level. A hotter
    setting that raises noise as much as signal buys nothing.
    """
    if not _require("arecord"):
        return
    tmp = tempfile.gettempdir()
    results = []

    print("\nThis measures noise, then speech, at three boost settings.")
    print("Speak normally at your usual distance when prompted.\n")

    for boost in (1, 2, 3):
        subprocess.run(["amixer", "-c", "0", "sset",
                        "Left Input Boost Mixer LINPUT1", str(boost)],
                       check=False, capture_output=True)
        subprocess.run(["amixer", "-c", "0", "sset",
                        "Right Input Boost Mixer RINPUT1", str(boost)],
                       check=False, capture_output=True)
        db_map = {1: "+13dB", 2: "+20dB", 3: "+29dB"}
        print(f"--- boost {boost} ({db_map[boost]}) ---")

        n = _capture(os.path.join(tmp, f"n{boost}.wav"), 3,
                     "  measuring noise - stay silent")
        if not n:
            continue
        noise = dict(_band_levels(n, ALSA_RATE))["speech 300-3k4"]

        input("  press Enter, then speak for 3s...")
        s = _capture(os.path.join(tmp, f"s{boost}.wav"), 3, "  speak now")
        if not s:
            continue
        sig  = dict(_band_levels(s, ALSA_RATE))["speech 300-3k4"]
        peak = _dbfs(max(abs(x) for x in s))

        snr = sig - noise
        clip = "  CLIPPING" if peak > -1 else ""
        print(f"  signal {sig:6.1f}  noise {noise:6.1f}  SNR {snr:5.1f} dB"
              f"  peak {peak:6.1f}{clip}\n")
        results.append((snr, boost, peak))

    if not results:
        print("no usable measurements")
        return

    usable = [r for r in results if r[2] < -3] or results
    best = max(usable)
    print(f"best SNR {best[0]:.1f} dB at boost {best[1]}")
    for b in ("Left Input Boost Mixer LINPUT1", "Right Input Boost Mixer RINPUT1"):
        subprocess.run(["amixer", "-c", "0", "sset", b, str(best[1])],
                       check=False, capture_output=True)
    print(f"applied boost {best[1]}")
    print("persist with: sudo alsactl store -f /var/lib/alsa/asound.state")

def run_clean():
    """Record, high-pass at 80Hz, and report headroom recovered.

    Everything below 80Hz is outside the speech band, so removing it costs
    nothing intelligible and buys back the headroom the rumble was eating.
    Writes both raw and filtered files for comparison.
    """
    import wave, struct
    if not (_require("arecord") and _require("aplay")):
        return
    tmp  = tempfile.gettempdir()
    raw  = os.path.join(tmp, "rsc_clean_raw.wav")
    out  = os.path.join(tmp, "rsc_clean_hp.wav")

    xs = _capture(raw, MIC_SECONDS, f"recording {MIC_SECONDS}s - speak now")
    if not xs:
        return

    xs, dc_offset = _demean(xs)
    lp = _onepole_lp(xs, 80, ALSA_RATE)
    hp = [a - b for a, b in zip(xs, lp)]

    print(f"\n  DC offset removed : {dc_offset:+.1f} LSB")
    print(f"  rms  before : {_dbfs(_rms(xs)):6.1f} dBFS")
    print(f"  rms  after  : {_dbfs(_rms(hp)):6.1f} dBFS")
    print(f"  peak before : {_dbfs(max(abs(x) for x in xs)):6.1f} dBFS")
    print(f"  peak after  : {_dbfs(max(abs(x) for x in hp)):6.1f} dBFS")

    print("\n  bands before -> after:")
    for (n, b4), (_, af) in zip(_band_levels(xs, ALSA_RATE),
                                _band_levels(hp, ALSA_RATE)):
        print(f"    {n}  {b4:6.1f} -> {af:6.1f}  ({af - b4:+5.1f} dB)")

    pk = max(abs(x) for x in hp) or 1
    scale = min(32767 * 0.7 / pk, 8.0)
    print(f"\n  normalising x{scale:.1f}")

    with wave.open(out, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ALSA_RATE)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * scale)))) for s in hp
        ))

    print("\n  playing raw...")
    subprocess.run(["aplay", "-q", "-D", ALSA_DEVICE, raw], check=False)
    time.sleep(0.4)
    print("  playing filtered...")
    subprocess.run(["aplay", "-q", "-D", ALSA_DEVICE, out], check=False)
    print(f"\n  raw      {raw}\n  filtered {out}")


AUDIO_RUNNERS = {
    "speaker": run_speaker,
    "mic":     run_mic,
    "meter":   run_meter,
    "gain":    run_gain,
    "status":  run_status,
    "noise":   run_noise,
    "tune":    run_tune,
    "clean":   run_clean,
}

def _fft(a):
    """Iterative radix-2 FFT. Pure stdlib, in-place Cooley-Tukey."""
    import cmath
    a = [complex(x) for x in a]
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        wl = cmath.exp(-2j * cmath.pi / length)
        half = length // 2
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(i, i + half):
                u, v = a[k], a[k + half] * w
                a[k], a[k + half] = u + v, u - v
                w *= wl
        length <<= 1
    return a


def _spectrum(xs, rate, size=8192, avg=6):
    """Averaged magnitude spectrum. Returns (freqs, mags_db)."""
    import math
    if len(xs) < size:
        return [], []
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (size - 1)) for i in range(size)]
    step = max((len(xs) - size) // max(avg - 1, 1), 1)
    acc = [0.0] * (size // 2)
    used = 0
    for w in range(avg):
        off = w * step
        if off + size > len(xs):
            break
        seg = [xs[off + i] * win[i] for i in range(size)]
        spec = _fft(seg)
        for i in range(size // 2):
            acc[i] += abs(spec[i])
        used += 1
    if not used:
        return [], []
    mags = [_dbfs(2 * m / used / (size / 2)) for m in acc]
    freqs = [i * rate / size for i in range(size // 2)]
    return freqs, mags


def _find_peaks(freqs, mags, lo=60, hi=20000, span=40, guard=4, thresh=6.0):
    """Peaks standing above their local neighbourhood, not the global median.

    Comparing against a global median finds only the spectrum's overall tilt.
    Comparing against nearby bins finds actual discrete tones.
    """
    out = []
    n = len(mags)
    for i in range(n):
        f = freqs[i]
        if f < lo or f > hi:
            continue
        a, b = max(0, i - span), min(n, i + span + 1)
        nb = [mags[k] for k in range(a, b) if abs(k - i) > guard]
        if not nb:
            continue
        local = sorted(nb)[len(nb) // 2]
        prom = mags[i] - local
        if prom >= thresh and mags[i] >= max(mags[max(0, i-2):i+3]):
            out.append((f, mags[i], prom))
    out.sort(key=lambda t: -t[2])
    # Drop near-duplicates within 5% in frequency.
    keep = []
    for f, m, p in out:
        if all(abs(f - g) / max(f, 1) > 0.05 for g, _, _ in keep):
            keep.append((f, m, p))
    return keep[:8]


def run_probe():
    """Find discrete tones in the noise floor and identify their harmonics."""
    if not _require("arecord"):
        return
    path = os.path.join(tempfile.gettempdir(), "rsc_probe.wav")
    xs = _capture(path, 4, "recording 4s - stay SILENT, do not move")
    if not xs:
        return
    xs, dc = _demean(xs)
    print(f"  DC offset {dc:+.0f} LSB removed")
    print("  computing spectrum...")

    freqs, mags = _spectrum(xs, ALSA_RATE)
    if not freqs:
        print("  not enough samples")
        return
    peaks = _find_peaks(freqs, mags)

    if not peaks:
        print("\n  no discrete tones found - noise is broadband.")
        print("  Nothing here to chase electrically.")
        return

    print("\n  discrete tones (prominence above local neighbours):")
    for f, m, p in peaks:
        print(f"    {f:8.0f} Hz   {m:6.1f} dBFS   +{p:4.1f} dB  {'#' * min(int(p), 30)}")

    fs = sorted(f for f, _, _ in peaks)
    best_hits, spacing = 0, None
    for i in range(len(fs)):
        for j in range(i + 1, len(fs)):
            d = fs[j] - fs[i]
            if d < 200:
                continue
            hits = sum(1 for a in fs for b in fs if b > a
                       and abs((b - a) / d - round((b - a) / d)) < 0.08
                       and round((b - a) / d) >= 1)
            if hits > best_hits:
                best_hits, spacing = hits, d

    print("\n  interpretation:")
    if spacing and best_hits >= 3:
        print(f"    Lines evenly spaced by ~{spacing:.0f} Hz, with no fundamental")
        print("    in band. That is aliasing: a switching source above Nyquist")
        print("    folding its harmonics down. Candidate switching frequencies:")
        for n in range(5, 9):
            print(f"      {(n * ALSA_RATE + spacing) / 1000:7.1f} kHz  "
                  f"or {(n * ALSA_RATE - spacing) / 1000:7.1f} kHz")
        print("    Aliased content cannot be filtered digitally - it is in band.")
        print("    Fix at the input: RC filter before the ADC, or decoupling")
        print("    and shielding at the converter.")
        lo_tones = [f for f, _, _ in peaks if f < 4000]
        if not lo_tones:
            print("    All tones sit above 4kHz, so the speech band is clean.")
            print("    Resampling to 16k removes them before STT sees them.")
    else:
        base = peaks[0][0]
        print(f"    Strongest tone {base:.0f} Hz, no regular structure found.")
    print(f"\n  file kept at {path}")


def run_isolate():
    """Measure the noise floor under different electrical loads.

    If the converter is pulse-skipping, its noise changes when the load
    changes. Drawing more current pushes it into continuous conduction,
    which is quieter. This tells you whether the whine is load-dependent
    and therefore fixable, or fixed and needing filtering.
    """
    if not _require("arecord"):
        return
    tmp = tempfile.gettempdir()

    def measure(label):
        xs = _capture(os.path.join(tmp, f"iso_{label}.wav"), 3, f"  measuring...")
        if not xs:
            return None
        xs, _ = _demean(xs)
        by = dict(_band_levels(xs, ALSA_RATE))
        freqs, mags = _spectrum(xs, ALSA_RATE, size=4096, avg=4)
        peaks = _find_peaks(freqs, mags) if freqs else []
        return {
            "rms":   _dbfs(_rms(xs)),
            "hiss":  by["hiss      >8kHz"],
            "speech": by["speech 300-3k4"],
            "peak":  peaks[0] if peaks else None,
        }

    results = {}

    print("\n--- A: baseline, nothing driven ---")
    results["baseline"] = measure("a")

    print("\n--- B: playback path disconnected ---")
    for c in ("Left Output Mixer PCM", "Right Output Mixer PCM"):
        subprocess.run(["amixer", "-c", "0", "sset", c, "off"],
                       check=False, capture_output=True)
    results["no-playback"] = measure("b")
    for c in ("Left Output Mixer PCM", "Right Output Mixer PCM"):
        subprocess.run(["amixer", "-c", "0", "sset", c, "on"],
                       check=False, capture_output=True)

    print("\n--- C: servos holding at neutral (servo rail) ---")
    chip = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)
            lgpio.tx_servo(chip, pin, SERVO_STOP)
        time.sleep(0.5)
        best = None
        for k in range(3):
            m = measure(f"c{k}")
            print(f"    pass {k+1}: rms {m['rms']:.1f}  hiss {m['hiss']:.1f}")
            if m and (best is None or m["hiss"] > best["hiss"]):
                best = m
        results["servos"] = best
        for pin in SERVO_PINS:
            _servo_off(chip, pin)
        time.sleep(0.5)
        print("\n--- C2: servo lines claimed, pulses ceased ---")
        results["servos-idle"] = measure("f")
        
    except Exception as e:
        print(f"  servos unavailable - {e}")
    finally:
        if chip is not None:
            try:
                for pin in SERVO_PINS:
                    _servo_off(chip, pin)
                    lgpio.gpio_free(chip, pin)
                lgpio.gpiochip_close(chip)
            except Exception:
                pass

    if os.geteuid() == 0:
        print("\n--- D: ring lit white (servo rail, same as servos) ---")
        ring = _open_ring()
        if ring:
            try:
                ring.fill((0, 0, 0, 120))
                ring.show()
                time.sleep(0.5)
                results["ring"] = measure("d")
            finally:
                try:
                    ring.fill((0, 0, 0, 0))
                    ring.show()
                    ring.deinit()
                except Exception:
                    pass
    else:
        print("\n--- D: skipped, ring needs root ---")

    base = results.get("baseline")
    print("\n--- A': baseline repeated, to measure drift ---")
    results["baseline-2"] = measure("e")
    if not base:
        print("\nno baseline - cannot compare")
        return

    print("\n  condition        rms    speech    hiss   d-rms  d-spch  d-hiss")
    print("  " + "-" * 66)
    for name, r in results.items():
        if not r:
            continue
        print(f"  {name:<14} {r['rms']:6.1f}  {r['speech']:6.1f}  {r['hiss']:6.1f}  "
              f"{r['rms']-base['rms']:+6.1f}  {r['speech']-base['speech']:+6.1f}  "
              f"{r['hiss']-base['hiss']:+6.1f}")

    print("\n  reading this:")
    b2 = results.get("baseline-2")
    if b2:
        drift = b2["rms"] - base["rms"]
        if abs(drift) > 3:
            print(f"    ** DRIFT {drift:+.1f} dB between first and last baseline.")
            print("       Conditions were measured in sequence, so this drift")
            print("       contaminates every delta above. Treat them as void.")
            print("       Re-run when the unit has been powered for 10+ minutes.")
        else:
            print(f"    Baseline repeatable within {abs(drift):.1f} dB - deltas are sound.")
    print("    A change of more than 3dB is real; less is measurement scatter.")
    np = results.get("no-playback")
    if np and np["hiss"] - base["hiss"] < -4:
        print("    * Playback path is a major hiss source. The class-D amp on")
        print("      the HAT couples into the mics. With full duplex you will")
        print("      need this filtered or the amp better decoupled.")
    for k in ("servos", "ring"):
        r = results.get(k)
        if r and abs(r["rms"] - base["rms"]) > 3:
            direction = "louder" if r["rms"] > base["rms"] else "QUIETER"
            print(f"    * Load from {k} makes it {direction}.")
            if r["rms"] < base["rms"]:
                print("      Quieter under load confirms pulse-skipping: the")
                print("      converter runs cleanly when it has work to do.")
                print("      Fix: add a dummy load, or a converter with forced PWM.")
    if all(not r or abs(r["rms"] - base["rms"]) <= 3
           for k, r in results.items() if k != "baseline"):
        print("    * Nothing shifted the floor. The noise is load-independent,")
        print("      so filter it downstream rather than chasing the supply.")


AUDIO_RUNNERS["probe"]   = run_probe
AUDIO_RUNNERS["isolate"] = run_isolate

def _hiss(xs):
    """Level above 8kHz - the band where servo switching shows up."""
    lp = _onepole_lp(xs, 8000, ALSA_RATE)
    return _dbfs(_rms([a - b for a, b in zip(xs, lp)]))

def _servo_off(chip, pin):
    """Cease pulses. Zero is only valid once a wave exists, hence the guard."""
    try:
        lgpio.tx_servo(chip, pin, 0)
    except Exception:
        pass

def run_servo_null():
    """Find each servo's true neutral by listening for its H-bridge.

    A continuous-rotation servo at true neutral has zero error, so its
    controller idles and stays electrically quiet. Off neutral it drives,
    corrects and overshoots, and that switching couples into the mics.
    Minimum noise therefore marks minimum error.

    Watch the horn as it runs: this finds the electrical null, which is
    usually but not always the point of zero mechanical creep.
    """
    if not _require("arecord"):
        return
    tmp    = tempfile.gettempdir()
    widths = list(range(1400, 1606, 15))   # 1400..1600
    # widths = list(range(1450, 1556, 15))   # 1450..1555 in 15us steps
    passes = 4                              # hunting is intermittent

    chip = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        print(f"\nsweeping {len(widths)} widths x {passes} passes per servo.")
        print("Stay quiet and still. Watch the horns for creep.\n")

        for idx, pin in enumerate(SERVO_PINS):
            side = "left" if idx == 0 else "right"
            print(f"--- GPIO{pin} ({side}) ---")
            for other in SERVO_PINS:
                if other != pin:
                    _servo_off(chip, other)
            time.sleep(0.3)
            scores = []
            for us in widths:
                lgpio.tx_servo(chip, pin, us)
                time.sleep(0.4)
                vals = []
                for k in range(passes):
                    xs = _capture(os.path.join(tmp, f"null_{pin}_{us}_{k}.wav"), 2)
                    if not xs:
                        continue
                    xs, _ = _demean(xs)
                    vals.append(_hiss(xs))
                worst = sorted(vals)[len(vals) // 2] if vals else -999.0
                scores.append((worst, us))
                print(f"    {us}us   hiss {worst:6.1f} dBFS  {_bar(worst, -60, -25, 30)}")
            _servo_off(chip, pin)

            if not scores:
                continue
            quietest, best_us = min(scores)
            loudest = max(s for s, _ in scores)
            if best_us in (widths[0], widths[-1]):
                print(f"  ** minimum at the edge of the sweep - the true null")
                print(f"     probably lies beyond {best_us}us. Widen the range.")
            print(f"\n  quietest at {best_us}us  ({quietest:.1f} dBFS)")
            print(f"  range across sweep: {loudest - quietest:.1f} dB")
            if loudest - quietest < 6:
                print("  Too flat to call - the servo never hunted audibly.")
                print("  Either it is already well trimmed, or it is unpowered.")
            else:
                trim = best_us - SERVO_STOP
                print(f"  trim vs SERVO_STOP({SERVO_STOP}): {trim:+d}us")
                print(f"  suggested: SERVO_{'L' if idx == 0 else 'R'}_NULL = {best_us}")
            print()

    except Exception as e:
        print(f"servo null failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["servo_null"] = run_servo_null

def _motor_tone(chip, pin, us, secs=2):
    """Drive one servo and return the dominant tone frequency, in Hz.

    Motor and gear-mesh frequency scales linearly with rotation speed, so
    this is a proxy for how fast the servo is actually turning.
    """
    lgpio.tx_servo(chip, pin, us)
    time.sleep(0.6)                       # let it reach steady speed
    xs = _capture(os.path.join(tempfile.gettempdir(), f"match_{pin}_{us}.wav"), secs)
    _servo_off(chip, pin)
    time.sleep(0.3)
    if not xs:
        return None, None
    xs, _ = _demean(xs)
    freqs, mags = _spectrum(xs, ALSA_RATE, size=4096, avg=5)
    peaks = _find_peaks(freqs, mags, lo=150, hi=6000, thresh=4.0)
    if not peaks:
        return None, None
    return peaks[0][0], peaks[0][2]


def run_servo_match():
    """Match the two servos' speeds by matching their motor tone.

    Drives the left servo forward, measures its dominant tone, then sweeps
    the right servo until its tone matches. Equal tone means equal motor
    speed, so the resulting width is the true speed-matched trim.

    Drive one at a time throughout - two running servos would make the
    dominant peak ambiguous.
    """
    if not _require("arecord"):
        return
    chip = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        lpin, rpin = SERVO_PINS
        print("\nMeasuring motor tone. Keep the room quiet - the servo should")
        print("be the loudest thing present. Wheels should turn freely.\n")

        print(f"--- reference: left GPIO{lpin} at {SERVO_L_FWD}us ---")
        refs = []
        for k in range(2):
            f, p = _motor_tone(chip, lpin, SERVO_L_FWD)
            print(f"    pass {k+1}: {f:.0f} Hz (+{p:.0f} dB)" if f
                  else f"    pass {k+1}: no tone found")
            if f:
                refs.append(f)
        if not refs:
            print("\n  No motor tone detected. Either the servo is not turning,")
            print("  or it is quieter than the room. Try a quieter environment.")
            return
        ref = sum(refs) / len(refs)
        spread = max(refs) - min(refs) if len(refs) > 1 else 0
        print(f"\n  reference tone {ref:.0f} Hz (spread {spread:.0f} Hz)")
        if spread > ref * 0.1:
            print("  Spread is high - treat the result as indicative only.")

        centre = SERVO_R_FWD
        cands  = list(range(centre - 60, centre + 61, 15))
        print(f"\n--- sweeping right GPIO{rpin}, {cands[0]}..{cands[-1]}us ---")
        results = []
        for us in cands:
            f, p = _motor_tone(chip, rpin, us)
            if f is None:
                print(f"    {us}us   no tone")
                continue
            err = f - ref
            print(f"    {us}us   {f:6.0f} Hz   {err:+6.0f} Hz from reference")
            results.append((abs(err), us, f))

        if not results:
            print("\n  No tones found on the right servo.")
            return

        _, best_us, best_f = min(results)
        print(f"\n  closest match: {best_us}us at {best_f:.0f} Hz")
        print(f"  reference was {ref:.0f} Hz - error {best_f - ref:+.0f} Hz "
              f"({100 * (best_f - ref) / ref:+.1f}%)")
        print(f"\n  current  SERVO_R_TRIM = {SERVO_R_TRIM}")
        print(f"  suggested SERVO_R_TRIM = {1400 - best_us}")
        print("\n  Verify by driving both forward and watching for drift.")
        print("  Tone matching equalises motor speed, not wheel slip.")

    except Exception as e:
        print(f"servo match failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["servo_match"] = run_servo_match

def run_servo_spin():
    """Test whether any spectral feature tracks rotation speed.

    Prerequisite for all acoustic servo sensing. Drives one servo at
    increasing offsets from neutral and prints the top peaks at each. If a
    peak family scales roughly in proportion to offset, it tracks speed and
    can be used as a tachometer. If every peak sits still, the sound is
    resonance rather than rotation, and acoustic speed sensing will not work.
    """
    if not _require("arecord"):
        return
    nulls   = {SERVO_PINS[0]: 1495, SERVO_PINS[1]: 1480}   # from servo_null
    offsets = [40, 70, 100, 140, 180, 220]

    chip = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        for pin in SERVO_PINS:
            for other in SERVO_PINS:
                if other != pin:
                    _servo_off(chip, other)
            null = nulls.get(pin, SERVO_STOP)
            print(f"\n--- GPIO{pin}, null {null}us, forward sweep ---")
            print("  offset   width     top three peaks (Hz, prominence)")
            rows = []
            for off in offsets:
                us = null + off
                lgpio.tx_servo(chip, pin, us)
                time.sleep(0.8)
                xs = _capture(os.path.join(tempfile.gettempdir(),
                                           f"spin_{pin}_{us}.wav"), 2)
                _servo_off(chip, pin)
                time.sleep(0.4)
                if not xs:
                    continue
                xs, _ = _demean(xs)
                freqs, mags = _spectrum(xs, ALSA_RATE, size=4096, avg=5)
                pk = _find_peaks(freqs, mags, lo=100, hi=8000, thresh=4.0)[:3]
                desc = "  ".join(f"{f:5.0f}(+{p:.0f})" for f, _, p in pk) or "none"
                print(f"  {off:+5d}   {us:5d}     {desc}")
                if pk:
                    rows.append((off, [f for f, _, _ in pk]))

            if len(rows) < 4:
                print("  too few measurements to judge")
                continue

            # A speed-tracking peak keeps f/offset roughly constant.
            print("\n  f/offset ratio for the strongest peak at each step:")
            ratios = [(off, fs[0] / off) for off, fs in rows]
            for off, r in ratios:
                print(f"    {off:+5d}   {r:6.2f} Hz per us")
            vals = [r for _, r in ratios]
            spread = (max(vals) - min(vals)) / (sum(vals) / len(vals))
            print(f"\n  ratio spread {100 * spread:.0f}%")
            if spread < 0.25:
                print("  -> Tracks speed. This peak is usable as a tachometer.")
            else:
                first, last = rows[0][1][0], rows[-1][1][0]
                exp = rows[-1][0] / rows[0][0]
                got = last / first if first else 0
                print(f"  -> Does not track. Speed rose {exp:.1f}x, tone {got:.1f}x.")
                print("     The sound is resonance, not rotation. Acoustic")
                print("     speed sensing will not work on this servo.")

    except Exception as e:
        print(f"servo spin failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["servo_spin"] = run_servo_spin

def _log_spectrum(xs, lo=200.0, hi=8000.0, n=240):
    """Magnitude spectrum resampled onto a log-frequency grid.

    On a log axis, a change in rotation speed becomes a pure translation,
    so aligning two spectra by sliding one against the other measures the
    speed ratio directly - no peak picking involved.
    """
    import math
    freqs, mags = _spectrum(xs, ALSA_RATE, size=4096, avg=5)
    if len(freqs) < 4:
        return [], 0.0
    df   = freqs[1] - freqs[0]
    step = (math.log(hi) - math.log(lo)) / (n - 1)
    out  = []
    for i in range(n):
        j  = (lo * math.exp(i * step)) / df
        j0 = int(j)
        if j0 + 1 >= len(mags):
            out.append(mags[-1])
        else:
            frac = j - j0
            out.append(mags[j0] * (1 - frac) + mags[j0 + 1] * frac)
    return out, step


def _corr(a, b):
    n = len(a)
    if n < 16:
        return -2.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return -2.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5


def _best_shift(a, b, maxshift=90):
    """Bins by which b sits above a, plus the correlation at that shift."""
    n = min(len(a), len(b))
    best = (-2.0, 0)
    for k in range(-maxshift, maxshift + 1):
        if k >= 0:
            x, y = a[:n - k], b[k:n]
        else:
            x, y = a[-k:n], b[:n + k]
        c = _corr(x, y)
        if c > best[0]:
            best = (c, k)
    return best


def run_servo_track():
    """Does the servo's sound scale with speed? Measured by spectral shift.

    Records at several offsets from neutral, then aligns each spectrum
    against the slowest one on a log-frequency axis. If the sound comes from
    rotation, the alignment shift should match the speed ratio. If it comes
    from fixed resonances, the best alignment sits near zero shift however
    fast the servo turns.
    """
    import math
    if not _require("arecord"):
        return
    nulls   = {SERVO_PINS[0]: 1495, SERVO_PINS[1]: 1450}
    offsets = [60, 90, 120, 160, 200, 240]

    chip = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        for pin in SERVO_PINS:
            for other in SERVO_PINS:
                if other != pin:
                    _servo_off(chip, other)
            null = nulls.get(pin, SERVO_STOP)
            print(f"\n--- GPIO{pin}, null {null}us ---")

            specs, step = [], 0.0
            for off in offsets:
                us = null + off
                lgpio.tx_servo(chip, pin, us)
                time.sleep(0.8)
                xs = _capture(os.path.join(tempfile.gettempdir(),
                                           f"trk_{pin}_{us}.wav"), 2)
                _servo_off(chip, pin)
                time.sleep(0.4)
                if not xs:
                    continue
                xs, _ = _demean(xs)
                sp, step = _log_spectrum(xs)
                if sp:
                    specs.append((off, sp))
                    print(f"  captured offset {off:+4d} ({us}us)")

            if len(specs) < 4:
                print("  too few captures to judge")
                continue

            base_off, base = specs[0]
            print(f"\n  aligning each spectrum against offset {base_off:+d}")
            print("  offset   speed x   measured x   corr   verdict")
            hits = 0
            for off, sp in specs[1:]:
                c, k = _best_shift(base, sp)
                measured = math.exp(k * step)
                expected = off / base_off
                ok = abs(measured - expected) / expected < 0.30 and c > 0.5
                hits += 1 if ok else 0
                mark = "tracks" if ok else ("flat" if abs(k) < 4 else "no")
                print(f"  {off:+5d}   {expected:6.2f}   {measured:9.2f}   "
                      f"{c:5.2f}   {mark}")

            print()
            if hits >= 3:
                print("  -> The spectrum shifts with speed. A tachometer is")
                print("     feasible: track the shift, not any single peak.")
            elif hits >= 1:
                print("  -> Partial tracking. Some speed-dependent content, but")
                print("     mixed with fixed resonances. Marginal at best.")
            else:
                print("  -> No consistent shift. The sound is dominated by fixed")
                print("     resonances, so it carries no speed information.")

    except Exception as e:
        print(f"servo track failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["servo_track"] = run_servo_track

def _capture_stereo(path, secs, prompt=None):
    """Record and return (left, right) as separate lists."""
    import array, wave
    if prompt:
        print(prompt)
    r = subprocess.run(
        ["arecord", "-D", ALSA_DEVICE, "-f", ALSA_FORMAT,
         "-r", str(ALSA_RATE), "-c", "2", "-d", str(secs), path],
        check=False, capture_output=True,
    )
    if r.returncode != 0:
        print("  capture failed")
        return None, None
    with wave.open(path, "rb") as w:
        rate, chans, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    s = array.array("h")
    s.frombytes(raw)
    if chans < 2:
        return None, None
    skip = int(rate * 0.25)
    return list(s[0::2])[skip:], list(s[1::2])[skip:]


def _xcorr_lag(a, b, maxlag=16):
    """Sample lag at which b best matches a, with parabolic interpolation.

    The HAT's mics sit ~65mm apart, so the largest possible acoustic delay
    is about 190us - roughly 9 samples at 48kHz. Anything beyond that is
    not a direction, it is noise.
    """
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    scores = []
    for k in range(-maxlag, maxlag + 1):
        if k >= 0:
            x, y = a[:n - k], b[k:n]
        else:
            x, y = a[-k:n], b[:n + k]
        scores.append((_corr(x, y), k))
    best_c, best_k = max(scores)
    idx = best_k + maxlag
    if 0 < idx < len(scores) - 1:
        y0, y1, y2 = scores[idx - 1][0], scores[idx][0], scores[idx + 1][0]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-9:
            best_k += 0.5 * (y0 - y2) / denom
    return best_k, best_c


def run_stereo():
    """Does using both mics help? Measure rather than assume.

    Summing two mics gains at most 3dB, and only against noise that differs
    between channels. Electrical noise entering through a shared supply and
    a shared ADC is usually identical in both, and summing it gains nothing.
    This measures the correlation for silence and for speech separately, and
    reports what sum and difference actually achieve.
    """
    if not _require("arecord"):
        return
    tmp = tempfile.gettempdir()

    def analyse(label, l, r):
        l, _ = _demean(l)
        r, _ = _demean(r)
        n = min(len(l), len(r))
        l, r = l[:n], r[:n]
        summed = [(x + y) / 2 for x, y in zip(l, r)]
        diffed = [(x - y) / 2 for x, y in zip(l, r)]
        c = _corr(l, r)
        lag, lagc = _xcorr_lag(l, r)
        band = lambda xs: dict(_band_levels(xs, ALSA_RATE))["speech 300-3k4"]
        print(f"\n  {label}")
        print(f"    left        {_dbfs(_rms(l)):6.1f} dBFS   speech band {band(l):6.1f}")
        print(f"    right       {_dbfs(_rms(r)):6.1f} dBFS   speech band {band(r):6.1f}")
        print(f"    sum  (L+R)/2 {_dbfs(_rms(summed)):6.1f} dBFS  speech band {band(summed):6.1f}")
        print(f"    diff (L-R)/2 {_dbfs(_rms(diffed)):6.1f} dBFS  speech band {band(diffed):6.1f}")
        print(f"    correlation {c:+.3f}   best lag {lag:+.2f} samples (r={lagc:+.2f})")
        return {"corr": c, "l": band(l), "sum": band(summed), "diff": band(diffed)}

    l, r = _capture_stereo(os.path.join(tmp, "st_quiet.wav"), 3,
                           "recording 3s of silence - stay quiet")
    if l is None:
        return
    quiet = analyse("silence", l, r)

    input("\n  press Enter, then speak normally for 3s...")
    l, r = _capture_stereo(os.path.join(tmp, "st_speech.wav"), 3, "  speak now")
    if l is None:
        return
    speech = analyse("speech", l, r)
    if speech["l"] < quiet["l"] + 3:
        print("\n  ** speech barely exceeded the silence recording. Either the")
        print("     'silence' capture picked up something, or you spoke too")
        print("     quietly. The SNR figures below are unreliable - re-run.")

    print("\n  what this means:")
    if quiet["corr"] > 0.7:
        print(f"    Noise correlation {quiet['corr']:.2f} - the noise is nearly")
        print("    identical in both channels, so it is common-mode: electrical,")
        print("    arriving through the shared supply or ADC rather than the air.")
        print("    Summing will not reduce it.")
        if speech["corr"] < quiet["corr"] - 0.15:
            print("    Speech correlates less than the noise, so the difference")
            print("    channel may be worth exploring - it cancels common-mode.")
    elif quiet["corr"] < 0.3:
        print(f"    Noise correlation {quiet['corr']:.2f} - largely independent")
        print("    between channels, which is what mic self-noise looks like.")
        print("    Summing should give close to the full 3dB improvement.")
    else:
        print(f"    Noise correlation {quiet['corr']:.2f} - mixed. Partial gain")
        print("    from summing, somewhere below 3dB.")

    gain = (speech["sum"] - quiet["sum"]) - (speech["l"] - quiet["l"])
    print(f"\n    Measured SNR change from summing: {gain:+.1f} dB")
    if gain > 1.5:
        print("    Worth doing - record stereo and sum in the pipeline.")
    elif gain > -1.0:
        print("    No meaningful gain. Single channel is simpler and equal.")
    else:
        print("    Summing makes it worse - the channels are partly out of phase.")


AUDIO_RUNNERS["stereo"] = run_stereo

def run_servo_side():
    """Can the mic pair tell the left servo from the right?

    Drives each servo alone and measures inter-channel delay and level
    difference. Airborne sound gives a consistent delay whose sign flips
    between servos. If both read near zero, the noise is reaching the mics
    through the chassis rather than the air, and direction is unrecoverable.
    """
    if not _require("arecord"):
        return
    nulls = {SERVO_PINS[0]: 1495, SERVO_PINS[1]: 1450}
    tmp   = tempfile.gettempdir()
    chip  = None
    out   = {}
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        for idx, pin in enumerate(SERVO_PINS):
            for other in SERVO_PINS:
                if other != pin:
                    _servo_off(chip, other)
            side = "left" if idx == 0 else "right"
            us   = nulls.get(pin, SERVO_STOP) + 200
            print(f"\n--- {side} servo (GPIO{pin}) at {us}us ---")
            lags, diffs = [], []
            for k in range(3):
                lgpio.tx_servo(chip, pin, us)
                time.sleep(0.6)
                l, r = _capture_stereo(os.path.join(tmp, f"side_{pin}_{k}.wav"), 2)
                _servo_off(chip, pin)
                time.sleep(0.3)
                if l is None:
                    continue
                l, _ = _demean(l)
                r, _ = _demean(r)
                # Restrict to the band where servo noise lives.
                hp = lambda xs: [a - b for a, b in
                                 zip(xs, _onepole_lp(xs, 1000, ALSA_RATE))]
                lh, rh = hp(l), hp(r)
                lag, c = _xcorr_lag(lh, rh)
                dlev = _dbfs(_rms(lh)) - _dbfs(_rms(rh))
                print(f"    pass {k+1}: lag {lag:+5.2f} samples (r={c:+.2f})   "
                      f"L-R level {dlev:+5.2f} dB")
                lags.append(lag)
                diffs.append(dlev)
            if lags:
                ml = sum(lags) / len(lags)
                md = sum(diffs) / len(diffs)
                spread = max(lags) - min(lags)
                print(f"  mean lag {ml:+.2f} samples (spread {spread:.2f}), "
                      f"mean level diff {md:+.2f} dB")
                out[side] = (ml, md, spread)

        print("\n  verdict:")
        if len(out) < 2:
            print("    Not enough data.")
            return
        (ll, ld, ls), (rl, rd, rs) = out["left"], out["right"]
        sep_lag = abs(ll - rl)
        sep_lev = abs(ld - rd)
        if sep_lag > max(ls, rs) and sep_lag > 0.5:
            print(f"    Lag separates the two servos by {sep_lag:.2f} samples,")
            print("    more than the scatter. Direction is recoverable.")
        elif sep_lev > 1.5:
            print(f"    Level difference separates them by {sep_lev:.2f} dB.")
            print("    Cruder than delay, but usable as a left/right cue.")
        else:
            print("    Neither delay nor level distinguishes them. The noise")
            print("    almost certainly travels through the chassis rather than")
            print("    the air, which erases direction. Nothing to recover here.")

    except Exception as e:
        print(f"servo side failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["servo_side"] = run_servo_side


def run_lag_profile():
    """Print correlation against lag, to see whether the peak is unique.

    A genuine acoustic delay produces a single peak within +/-9 samples
    (the mics are ~65mm apart). A periodic signal produces a comb of
    near-equal peaks, and "best lag" then picks one arbitrarily - which
    looks repeatable while meaning nothing.
    """
    if not _require("arecord"):
        return
    nulls = {SERVO_PINS[0]: 1495, SERVO_PINS[1]: 1450}
    tmp   = tempfile.gettempdir()
    chip  = None
    try:
        chip = lgpio.gpiochip_open(0)
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)

        for idx, pin in enumerate(SERVO_PINS):
            for other in SERVO_PINS:
                if other != pin:
                    _servo_off(chip, other)
            side = "left" if idx == 0 else "right"
            us   = nulls.get(pin, SERVO_STOP) + 200
            print(f"\n--- {side} servo (GPIO{pin}) at {us}us ---")
            lgpio.tx_servo(chip, pin, us)
            time.sleep(0.6)
            l, r = _capture_stereo(os.path.join(tmp, f"lag_{pin}.wav"), 3)
            _servo_off(chip, pin)
            if l is None:
                continue
            l, _ = _demean(l)
            r, _ = _demean(r)
            hp = lambda xs: [a - b for a, b in
                             zip(xs, _onepole_lp(xs, 1000, ALSA_RATE))]
            lh, rh = hp(l), hp(r)
            n = min(len(lh), len(rh))
            lh, rh = lh[:n], rh[:n]

            # Correct the channel gain imbalance before comparing.
            gl, gr = _rms(lh), _rms(rh)
            if gl > 0 and gr > 0:
                rh = [x * (gl / gr) for x in rh]
                print(f"  channel imbalance {_dbfs(gl) - _dbfs(gr):+.2f} dB, corrected")

            print("  lag   corr")
            best, peaks = (-2.0, 0), []
            for k in range(-24, 25):
                if k >= 0:
                    x, y = lh[:n - k], rh[k:n]
                else:
                    x, y = lh[-k:n], rh[:n + k]
                c = _corr(x, y)
                mark = "  <== physical limit" if abs(k) == 9 else ""
                bar = "#" * max(0, int((c + 0.5) * 30))
                print(f"  {k:+4d}  {c:+.3f}  {bar}{mark}")
                if c > best[0]:
                    best = (c, k)
                peaks.append((c, k))

            top = sorted(peaks, reverse=True)[:5]
            print(f"\n  best lag {best[1]:+d} at r={best[0]:+.3f}")
            print("  top five lags: " + ", ".join(f"{k:+d}({c:+.2f})" for c, k in top))
            
            cs = [c for c, _ in peaks]
            ks = [k for _, k in peaks]
            maxima = [ks[i] for i in range(1, len(cs) - 1)
                    if cs[i] > cs[i - 1] and cs[i] >= cs[i + 1] and cs[i] > 0.1]
            gaps = [b - a for a, b in zip(maxima, maxima[1:])]
            print(f"  local maxima at lags: {maxima}")
            if gaps:
                mean_gap = sum(gaps) / len(gaps)
                print(f"  gaps {gaps} -> period ~{mean_gap:.1f} samples "
                    f"({ALSA_RATE / mean_gap:.0f} Hz)")
            mirrored = any(-k in maxima for k in maxima if k != 0)
            if len(maxima) >= 3 or mirrored:
                print("  ** comb structure - the correlation is following a periodic")
                print("     tone, not an arrival delay. No direction information.")
            elif abs(best[1]) <= 9:
                print("  -> Single peak within the physical limit. Possibly a real")
                print("     delay - confirm the sign flips between the two servos.")

            if abs(best[1]) > 9:
                print("  ** best lag exceeds the 9-sample physical limit - this")
                print("     is not an acoustic delay.")
            if len(top) >= 3 and top[0][0] - top[2][0] < 0.08:
                print("  ** the top peaks are nearly equal - a comb, not a single")
                print("     peak. The correlation is locking onto a periodic tone,")
                print(f"     apparent spacing ~{spacing[0] if spacing else 0} samples.")
                print("     Repeatable, but carries no direction information.")
            elif abs(best[1]) <= 9 and top[0][0] - top[1][0] > 0.05:
                print("  -> Single dominant peak within the physical limit.")
                print("     This looks like a real acoustic delay.")

    except Exception as e:
        print(f"lag profile failed - {e}")
    finally:
        if chip is not None:
            for pin in SERVO_PINS:
                _servo_off(chip, pin)
                try:
                    lgpio.gpio_free(chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass


AUDIO_RUNNERS["lag_profile"] = run_lag_profile


# --- Registry -------------------------------------------------------------

BEHAVIOURS = {
    "print":       PrintOnly,
    "poll":        Poll,
    "solid":       SolidWhileHeld,
    "brightness":  BrightnessCycle,
    "tap_hold":    TapVsHold,
    "ring":        RingColour,
    "sweep":       RingSweep,
    "servo_hold":  ServoHold,
    "servo_cycle": ServoCycle,
    "servo_dir":   ServoHoldDir,
    "servo_sweep": ServoSweep,
    "mixed":       Mixed,
}

CHOICES = list(BEHAVIOURS.keys()) + list(AUDIO_RUNNERS.keys())

RING_BEHAVIOURS   = {"ring", "sweep", "mixed"}
SERVO_BEHAVIOURS  = {"servo_hold", "servo_cycle", "servo_dir", "servo_sweep",
                     "mixed"}
BLOCKING_BEHAVIOURS = {"poll", "servo_sweep"}   # own their loop, no pause()
NO_BUTTON_BEHAVIOURS = {"poll"}                 # claim GPIO23 themselves


# --- Helpers --------------------------------------------------------------

def _force_led_low():
    """Drive the LED pin low on exit and leave it there.

    Releasing a chardev line reverts the pin to input, which floats Q1's gate
    and lights the LED. pinctrl writes the pad registers directly, so its
    state survives process exit - it is the part that actually sticks. The
    lgpio write first covers the case where pinctrl is absent.

    The durable fix is a pull-down on Q1's gate; nothing here survives SIGKILL.
    """
    try:
        chip = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(chip, LED_PIN, 0)
        lgpio.gpio_free(chip, LED_PIN)
        lgpio.gpiochip_close(chip)
    except Exception:
        pass
    try:
        subprocess.run(
            ["pinctrl", "set", str(LED_PIN), "op", "dl"],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def _open_ring():
    """Import and construct the NeoPixel ring. Returns None on failure."""
    if os.geteuid() != 0:
        print("ring needs root - rerun with sudo, e.g.")
        print("  sudo /home/rsc/rsc-env/bin/python rsc_test.py ring")
        return None
    try:
        import board
        import neopixel
    except ImportError as e:
        print(f"ring unavailable - {e}")
        return None
    try:
        return neopixel.NeoPixel(
            board.D12, RING_COUNT,
            brightness=0.3,
            auto_write=False,
            pixel_order=neopixel.GRBW,
        )
    except Exception as e:
        print(f"ring init failed - {e}")
        return None


def _open_servos():
    """Open a chip handle and claim the servo lines. Returns handle or None."""
    try:
        chip = lgpio.gpiochip_open(0)
    except Exception as e:
        print(f"cannot open gpiochip0 - {e}")
        print("is your user in the 'gpio' group? check with: id -nG")
        return None
    try:
        for pin in SERVO_PINS:
            lgpio.gpio_claim_output(chip, pin, 0)
    except Exception as e:
        print(f"cannot claim servo pins {SERVO_PINS} - {e}")
        lgpio.gpiochip_close(chip)
        return None
    print(f"lgpio open - servos on GPIO {SERVO_PINS}")
    return chip


# --- Entry ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RSC hardware test rig")
    parser.add_argument(
        "behaviour",
        nargs="?",
        default="solid",
        choices=CHOICES,
        help="behaviour to run (default: solid)",
    )
    args = parser.parse_args()

    # Audio tests touch no GPIO at all - run and return before claiming lines.
    if args.behaviour in AUDIO_RUNNERS:
        AUDIO_RUNNERS[args.behaviour]()
        return

    atexit.register(_force_led_low)

    button = None
    if args.behaviour not in NO_BUTTON_BEHAVIOURS:
        button = Button(
            BUTTON_PIN,
            pull_up=False,
            bounce_time=BOUNCE_TIME,
            hold_time=HOLD_TIME,
        )
    led  = PWMLED(LED_PIN)
    ring = None
    chip = None

    if args.behaviour in RING_BEHAVIOURS:
        ring = _open_ring()

    if args.behaviour in SERVO_BEHAVIOURS:
        chip = _open_servos()

    if args.behaviour in SERVO_BEHAVIOURS:
        behaviour = BEHAVIOURS[args.behaviour](button, led, ring, chip=chip)
    else:
        behaviour = BEHAVIOURS[args.behaviour](button, led, ring)

    try:
        print(f"running '{args.behaviour}' - ctrl-c to stop")
        behaviour.attach()
        if args.behaviour not in BLOCKING_BEHAVIOURS:
            pause()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        # Servos first - never leave a line driving.
        if chip is not None:
            try:
                for pin in SERVO_PINS:
                    _servo_off(chip, pin)
                time.sleep(0.05)
                for pin in SERVO_PINS:
                    lgpio.gpio_free(chip, pin)
            except Exception:
                pass
            try:
                lgpio.gpiochip_close(chip)
            except Exception:
                pass

        if ring:
            try:
                ring.fill((0, 0, 0, 0))
                ring.show()
                ring.deinit()
            except Exception:
                pass

        try:
            led.off()
            led.close()
            if button is not None:
                button.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()