"""
tests/servos.py — Left + right servo motors via pigpio.

Subtests
--------
  hold    both forward while button held, stop on release
  cycle   tap cycles: forward → reverse → stop → repeat
  dir     press → forward, hold → reverse, release → stop
"""

import time
from signal import pause

from config import GPIO, Servo as SrvCfg, Button as BtnCfg


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Verify pigpiod is reachable and servo pins are driveable."""
    try:
        import pigpio
        pi = pigpio.pi()
        if not pi.connected:
            return False, "pigpiod not running — sudo systemctl start pigpiod"
        for pin in (GPIO.SERVO_LEFT, GPIO.SERVO_RIGHT):
            pi.set_servo_pulsewidth(pin, 0)
        pi.stop()
        return True, f"pigpiod OK — servo pins GPIO{GPIO.SERVO_LEFT} / GPIO{GPIO.SERVO_RIGHT} driveable"
    except Exception as e:
        return False, str(e)


# ── Shared helpers ────────────────────────────────────────────────────────────

class _ServoBase:
    def __init__(self, button, led, pi):
        self.button = button
        self.led    = led
        self.pi     = pi

    def _set(self, l_pw, r_pw):
        if self.pi and self.pi.connected:
            self.pi.set_servo_pulsewidth(GPIO.SERVO_LEFT,  l_pw)
            self.pi.set_servo_pulsewidth(GPIO.SERVO_RIGHT, r_pw)

    def _stop(self):
        self._set(SrvCfg.STOP, SrvCfg.STOP)
        time.sleep(0.1)
        if self.pi and self.pi.connected:
            for pin in (GPIO.SERVO_LEFT, GPIO.SERVO_RIGHT):
                self.pi.set_servo_pulsewidth(pin, 0)

    def on_press(self):   pass
    def on_release(self): pass
    def on_hold(self):    pass

    def attach(self):
        self.button.when_pressed  = self.on_press
        self.button.when_released = self.on_release
        self.button.when_held     = self.on_hold
        self.led.pulse()


class _Hold(_ServoBase):
    def on_press(self):
        print("servo → forward")
        self._set(SrvCfg.LEFT_FWD, SrvCfg.RIGHT_FWD)

    def on_release(self):
        print("servo → stop")
        self._stop()


class _Cycle(_ServoBase):
    _STATES = [
        (SrvCfg.LEFT_FWD, SrvCfg.RIGHT_FWD, "forward"),
        (SrvCfg.LEFT_REV, SrvCfg.RIGHT_REV, "reverse"),
        (SrvCfg.STOP,     SrvCfg.STOP,      "stop"),
    ]

    def __init__(self, button, led, pi):
        super().__init__(button, led, pi)
        self._index = 0

    def attach(self):
        super().attach()
        self._stop()

    def on_press(self):
        l_pw, r_pw, label = self._STATES[self._index]
        print(f"servo → {label}")
        if label == "stop":
            self._stop()
        else:
            self._set(l_pw, r_pw)
        self._index = (self._index + 1) % len(self._STATES)


class _Dir(_ServoBase):
    def on_press(self):
        print("servo → forward")
        self._set(SrvCfg.LEFT_FWD, SrvCfg.RIGHT_FWD)

    def on_hold(self):
        print("servo → reverse")
        self._set(SrvCfg.LEFT_REV, SrvCfg.RIGHT_REV)

    def on_release(self):
        print("servo → stop")
        self._stop()


_BEHAVIOURS = {
    "hold":  _Hold,
    "cycle": _Cycle,
    "dir":   _Dir,
}


# ── Run ───────────────────────────────────────────────────────────────────────

def run(subtest="hold"):
    import pigpio
    import subprocess
    import atexit
    from gpiozero import Button, PWMLED

    atexit.register(
        lambda: subprocess.run(
            ["pinctrl", "set", str(GPIO.LED_PWM), "op", "dl"], check=False
        )
    )

    pi = pigpio.pi()
    if not pi.connected:
        print("pigpiod not connected — run: sudo systemctl start pigpiod")
        return

    print(f"pigpiod connected — servos on GPIO{GPIO.SERVO_LEFT} / GPIO{GPIO.SERVO_RIGHT}")

    button = Button(
        GPIO.BUTTON,
        pull_up=False,
        bounce_time=BtnCfg.BOUNCE_TIME_S,
        hold_time=SrvCfg.HOLD_TIME_S,
    )
    led = PWMLED(GPIO.LED_PWM)

    behaviour = _BEHAVIOURS[subtest](button, led, pi)
    behaviour.attach()

    print(f"servo › {subtest} — ctrl-c to stop")
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
        for pin in (GPIO.SERVO_LEFT, GPIO.SERVO_RIGHT):
            pi.set_servo_pulsewidth(pin, 0)
        pi.stop()
