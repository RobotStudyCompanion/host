"""
tests/ring.py — 16× SKC6812 RGBW NeoPixel ring on GPIO12.

Subtests
--------
  colour  ring fills blue on press, clears on release
  sweep   single pixel sweeps around ring while held
"""

import threading
import time
from signal import pause

import board
import neopixel

from config import GPIO, Ring as RingCfg, Button as BtnCfg, Servo


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Attempt to initialise the ring and clear it."""
    try:
        ring = _init_ring()
        ring.fill(RingCfg.COLOUR_OFF)
        ring.show()
        return True, f"{RingCfg.COUNT}× SKC6812 ring on GPIO{GPIO.RING} initialised OK"
    except Exception as e:
        return False, str(e)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _init_ring():
    return neopixel.NeoPixel(
        board.D12,
        RingCfg.COUNT,
        brightness=RingCfg.BRIGHTNESS,
        auto_write=False,
        pixel_order=neopixel.GRBW,
    )


class _RingBase:
    def __init__(self, button, led, ring):
        self.button = button
        self.led    = led
        self.ring   = ring

    def on_press(self):   pass
    def on_release(self): pass

    def attach(self):
        self.button.when_pressed  = self.on_press
        self.button.when_released = self.on_release
        self.led.pulse()
        self.ring.fill(RingCfg.COLOUR_OFF)
        self.ring.show()


class _Colour(_RingBase):
    def on_press(self):
        print("pressed")
        self.ring.fill(RingCfg.COLOUR_PRESS)
        self.ring.show()

    def on_release(self):
        print("released")
        self.ring.fill(RingCfg.COLOUR_OFF)
        self.ring.show()


class _Sweep(_RingBase):
    _running = False

    def on_press(self):
        print("pressed — sweeping")
        self._running = True
        self.ring.fill(RingCfg.COLOUR_OFF)
        self.ring.show()

        def _sweep():
            prev = 0
            while self._running:
                for i in range(len(self.ring)):
                    if not self._running:
                        break
                    self.ring[prev] = RingCfg.COLOUR_OFF
                    self.ring[i]    = RingCfg.COLOUR_PRESS
                    self.ring.show()
                    prev = i
                    time.sleep(RingCfg.SWEEP_DELAY_S)

        threading.Thread(target=_sweep, daemon=True).start()

    def on_release(self):
        print("released")
        self._running = False
        self.ring.fill(RingCfg.COLOUR_OFF)
        self.ring.show()


_BEHAVIOURS = {
    "colour": _Colour,
    "sweep":  _Sweep,
}


# ── Run ───────────────────────────────────────────────────────────────────────

def run(subtest="colour"):
    import atexit
    import subprocess
    from gpiozero import Button, PWMLED

    atexit.register(
        lambda: subprocess.run(
            ["pinctrl", "set", str(GPIO.LED_PWM), "op", "dl"], check=False
        )
    )

    ring = _init_ring()
    button = Button(
        GPIO.BUTTON,
        pull_up=False,
        bounce_time=BtnCfg.BOUNCE_TIME_S,
        hold_time=Servo.HOLD_TIME_S,
    )
    led = PWMLED(GPIO.LED_PWM)

    behaviour = _BEHAVIOURS[subtest](button, led, ring)
    behaviour.attach()

    print(f"ring › {subtest} — ctrl-c to stop")
    try:
        pause()
    except KeyboardInterrupt:
        pass
    finally:
        button.close()
        led.off()
        subprocess.run(
            ["pinctrl", "set", str(GPIO.LED_PWM), "op", "dl"], check=False
        )
        try:
            ring.fill(RingCfg.COLOUR_OFF)
            ring.show()
        except Exception:
            pass
