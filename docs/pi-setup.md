# Pi-side setup

One-time steps to bring the RSC Pi from a fresh Raspberry Pi OS install to a
running `rsc-host` service on boot.

Assumes: Raspberry Pi OS Bookworm (or newer), user `pi` (adapt the paths and
service filename if you use a different username), the RSC Power/Peripheral
HAT fitted, and a working audio HAT (ReSpeaker 2-Mic pHAT or Adafruit Voice
Bonnet).

## 1. System prerequisites

```bash
sudo apt update
sudo apt install -y \
    python3-venv python3-pip \
    pigpio python3-pigpio \
    git \
    libportaudio2 \
    build-essential
```

## 2. Enable UART for the CYD front panel

The CYD dispatch link needs `/dev/serial0` as a full UART, without the
console attached. Edit `/boot/firmware/config.txt`:

```ini
enable_uart=1
```

Then remove `console=serial0,115200` (or similar) from
`/boot/firmware/cmdline.txt` if present. Reboot.

Verify:

```bash
ls -l /dev/serial0        # should exist and be a symlink
```

## 3. Enable pigpiod on boot

```bash
sudo systemctl enable --now pigpiod
sudo systemctl status pigpiod     # should be active (running)
```

## 4. Group membership

The service runs as the `pi` user, which needs access to GPIO, SPI, audio,
serial, and I2C:

```bash
sudo usermod -aG gpio,spi,audio,dialout,i2c pi
```

Log out and back in, or reboot, for the group membership to take effect.

## 5. Install the host package

```bash
cd ~
git clone https://github.com/RobotStudyCompanion/host.git
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[pi]"
```

Smoke test in the foreground:

```bash
export RSC_HOST_TOKEN=dev
export RSC_HOST_BACKEND=pi
export RSC_HOST_BIND=0.0.0.0    # so LAN clients can reach it
python -m rsc_host
```

You should see:

```
INFO rsc_host.hal.pi: PiServoBackend started (pins=...)
INFO rsc_host.hal.pi: PiRingBackend started (pixels=16, buffer=16)
INFO rsc_host.hal.pi: PiGpioInputBackend started (pins=(23,))
INFO rsc_host.hal.pi: PiGpioPwmBackend started (pins=(24,))
INFO rsc_host.hal.pi: PiSerialBackend started (device=/dev/serial0, baud=115200)
INFO rsc_host.hal.pi: PiAudioBackend started (...)
INFO rsc_host.peripherals.registry: peripherals ready: backend=pi, ...
INFO rsc_host.server: host serving on ws://0.0.0.0:8765
```

Ctrl-C to stop. If any backend fails, the log will name the specific one —
usually a pin conflict, missing group membership, or pigpiod not running.

## 6. Install the systemd unit

The unit is a *template* (`rsc-host@.service`); the `%i` after the `@`
picks up the username at enable time.

```bash
sudo cp ~/host/systemd/rsc-host@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Set a real token before enabling; the shipped default is CHANGE_ME.
sudo systemctl edit rsc-host@pi.service
```

In the editor, add:

```ini
[Service]
Environment=RSC_HOST_TOKEN=<your-real-token>
```

Save, then:

```bash
sudo systemctl enable --now rsc-host@pi.service
sudo systemctl status rsc-host@pi.service
journalctl -u rsc-host@pi.service -f
```

## 7. Reboot to confirm boot-order behaviour

```bash
sudo reboot
```

After the Pi comes back:

```bash
systemctl status pigpiod rsc-host@pi.service
```

Both should be active. `rsc-host` will have started *after* `pigpiod` per
the `After=` and `Requires=` directives in the unit.

## 8. Safe-restart behaviour

* If `pigpiod` dies, systemd restarts it, then restarts `rsc-host` (because
  it `Requires=` pigpiod). No manual intervention.
* If `rsc-host` itself crashes, systemd restarts it after 2 s. Rate-limited
  to 5 restarts per minute; if it fails harder than that, systemd gives up
  and reports failed status.

To force-stop or check failure state:

```bash
sudo systemctl stop rsc-host@pi.service
sudo systemctl reset-failed rsc-host@pi.service    # after crash-loop
```

## Troubleshooting

**"cannot connect to pigpiod"** — `sudo systemctl status pigpiod`. If stopped,
`sudo systemctl start pigpiod`. If disabled, step 3.

**Ring not lighting** — verify the wiring goes to J6 (M1_PWM header, BCM 12).
If the strip lights partially, try a longer buffer:
edit `PiRingBackend(pixel_count=16, buffer_size=24)` in
`rsc_host/peripherals/registry.py`.

**Servos twitching at startup** — the code parks each servo at 1500 µs neutral
in `PiServoBackend.start()`. If your servos treat 1500 as slow-forward rather
than stop, adjust `_SERVO_TIMINGS` and `_SERVO_STOP_US` in
`rsc_host/hal/pi.py`.

**Button LED stays dimly lit after shutdown** — gpiozero occasionally leaves
the line floating. `PiGpioPwmBackend.stop()` runs `pinctrl set <pin> op dl`
to force it low; verify `pinctrl` is installed (`which pinctrl`). If missing,
`sudo apt install raspi-utils`.

**No `/dev/serial0`** — step 2 wasn't done or the reboot didn't happen.

**Audio device not found** — `arecord -l` and `aplay -l` to list ALSA devices.
If needed, set `RSC_HOST_...` env vars for input/output device index in a
follow-up (audio verbs land in a later layer).
