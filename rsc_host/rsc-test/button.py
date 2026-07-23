"""
tests/button.py — Arcade button (GPIO23) + LED PWM (GPIO24).

Subtests
--------
  print       log press / release events only
  solid       breathes idle, goes solid on press
  brightness  each press steps through brightness levels
  tap_hold    distinguish quick tap from long hold
"""

import atexit
import subprocess
from signal import pause

from config import GPIO, Button as BtnCfg, Servo


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Verify GPIO pins are accessible. No hardware interaction."""
    try:
        from gpiozero import Button, PWMLED
        b = Button(GPIO.BUTTON, pull_up=False)
        l = PWMLED(GPIO.LED_PWM)
        b.close()
        l.off()
        return True, f"GPIO{GPIO.BUTTON} (button) and GPIO{GPIO.LED_PWM} (LED) accessible"
    except Exception as e:
        return False, str(e)


# ── Behaviour classes ─────────────────────────────────────────────────────────

class _Behaviour:
    def __init__(self, button, led):
        self.button = button
        self.led    = led

    def on_press(self):   pass
    def on_release(self): pass
    def on_hold(self):    pass

    def attach(self):
        self.button.when_pressed  = self.on_press
        self.button.when_released = self.on_release
        self.button.when_held     = self.on_hold


class _PrintOnly(_Behaviour):
    def on_press(self):   print("pressed")
    def on_release(self): print("released")


class _SolidWhileHeld(_Behaviour):
    def attach(self):
        super().attach()
        self.led.pulse()

    def on_press(self):
        print("pressed")
        self.led.on()

    def on_release(self):
        print("released")
        self.led.pulse()


class _BrightnessCycle(_Behaviour):
    _levels = [0.0, 0.25, 0.5, 0.75, 1.0]

    def __init__(self, button, led):
        super().__init__(button, led)
        self._index = 0

    def on_press(self):
        self._index = (self._index + 1) % len(self._levels)
        self.led.value = self._levels[self._index]
        print(f"brightness → {self._levels[self._index]:.0%}")


class _TapVsHold(_Behaviour):
    def on_press(self):   print("pressed")
    def on_release(self): print("released")
    def on_hold(self):    print("held")


_BEHAVIOURS = {
    "print":      _PrintOnly,
    "solid":      _SolidWhileHeld,
    "brightness": _BrightnessCycle,
    "tap_hold":   _TapVsHold,
}


# ── Run ───────────────────────────────────────────────────────────────────────

def run(subtest="solid"):
    from gpiozero import Button, PWMLED

    atexit.register(
        lambda: subprocess.run(
            ["pinctrl", "set", str(GPIO.LED_PWM), "op", "dl"], check=False
        )
    )

    button = Button(
        GPIO.BUTTON,
        pull_up=False,
        bounce_time=BtnCfg.BOUNCE_TIME_S,
        hold_time=Servo.HOLD_TIME_S,
    )
    led = PWMLED(GPIO.LED_PWM)

    behaviour = _BEHAVIOURS[subtest](button, led)
    behaviour.attach()

    print(f"button › {subtest} — ctrl-c to stop")
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
