# Pi-side setup

One-time steps to bring an RSC Pi from a fresh Raspberry Pi OS install to a
`rsc-host` service that starts on boot.

Assumes Raspberry Pi OS Trixie or newer on a Pi 4, user `rsc`, the RSC
Power/Peripheral HAT fitted, and a ReSpeaker 2-Mic HAT for audio.

> **Do not install or enable pigpiod.** An earlier version of this document
> told you to, and that was the cause of the timing problems that were once
> blamed on the hardware. See [Why not pigpio](#why-not-pigpio) below. If
> `pigpiod` is present on a machine you are fixing rather than building, mask
> it: `sudo systemctl mask pigpiod`.

## 1. System packages

```bash
sudo apt update
sudo apt install -y \
    python3-venv python3-pip \
    python3-lgpio \
    git \
    alsa-utils \
    libasound2-plugins \
    avahi-daemon \
    raspi-utils
```

No `pigpio`. No `libportaudio2`: the daemon drives ALSA directly through
`arecord` and `aplay` rather than through PortAudio, because PortAudio cannot
address `plughw` and negotiates its own sample rate, which on this codec
produces audible warble.

`raspi-utils` provides `pinctrl`, which the service uses on shutdown to hold
the arcade LED pin low.

Give the Pi a name; it doubles as its mDNS identity:

```bash
sudo hostnamectl set-hostname rsc-shiny
sudo systemctl enable --now avahi-daemon
```

## 2. UART for the CYD front panel

The panel needs `/dev/serial0` as a real UART with no console attached.

In `/boot/firmware/config.txt`:

```ini
enable_uart=1
```

Remove any `console=serial0,115200` from `/boot/firmware/cmdline.txt`. Reboot,
then check `ls -l /dev/serial0` resolves.

## 3. Group membership

```bash
sudo usermod -aG gpio,spi,audio,dialout,i2c rsc
```

Log out and back in. `gpio` covers both the chardev lines and the ring
helper's socket; `audio` covers the WM8960.

## 4. ALSA

The capture recipe was measured rather than guessed, and the daemon applies it
at every start, so nothing here is strictly required. Two settings matter
anyway:

```bash
# Proper resampling, rather than ALSA's crude default
sudo tee /etc/asound.conf > /dev/null <<'EOF'
defaults.pcm.rate_converter "speexrate_medium"
EOF

# The ReSpeaker installer masks this; without it nothing survives a reboot
sudo systemctl unmask alsa-restore
sudo systemctl enable alsa-restore
```

## 5. Get the code onto the Pi

Either clone, or use Syncthing if you will be iterating from a laptop.

```bash
cd ~
git clone https://github.com/RobotStudyCompanion/host.git rsc-host
```

With Syncthing, ignore these on both sides or the sync will fight you:

```
.venv
rsc-env
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
.stfolder
.stversions
```

**`__pycache__` is not optional.** Syncthing preserves source mtimes, and
Python invalidates its bytecode cache on mtime plus size, so a freshly synced
file can look older than its own `.pyc` and be silently ignored. If a change
appears not to take effect, this is the first thing to check:

```bash
sudo find /home/rsc/rsc-host -name __pycache__ -type d -exec rm -rf {} +
sudo systemctl restart rsc-host@rsc
```

## 6. Python environment

```bash
python3 -m venv --system-site-packages ~/rsc-env
~/rsc-env/bin/pip install pydantic websockets numpy
```

Optional, each failing soft if absent:

```bash
~/rsc-env/bin/pip install pyserial-asyncio   # CYD front panel
~/rsc-env/bin/pip install zeroconf           # mDNS service advertisement
~/rsc-env/bin/pip install scipy              # sharper high-pass filter
```

The ring helper additionally needs `rpi_ws281x` and
`adafruit-circuitpython-neopixel`, installed in whichever interpreter runs it.

**Only one virtualenv.** Do not also `pip install -e .` into a second one. A
package installed into site-packages shadows the source tree, and the daemon
will run the installed copy while you edit the other, which has already cost
a debugging session on this project.

Smoke test in the foreground:

```bash
cd ~/rsc-host
RSC_HOST_BACKEND=pi RSC_HOST_RING_MODE=off RSC_HOST_TOKEN=dev \
  ~/rsc-env/bin/python -m rsc_host
```

The line that matters is `gpiozero pin factory: LGPIOFactory`. If it says
anything else, something has pulled in pigpio.

## 7. systemd units

Two units. `rsc-ring.service` runs as root and owns the NeoPixel ring;
`rsc-host@.service` runs as `rsc` and owns everything else.

```bash
sudo cp ~/rsc-host/systemd/rsc-ring.service /etc/systemd/system/
sudo cp ~/rsc-host/systemd/rsc-host@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rsc-ring.service
sudo systemctl enable --now rsc-host@rsc.service
```

Put the token in a drop-in rather than the unit, so reinstalling the unit does
not regenerate it:

```bash
sudo systemctl edit rsc-host@rsc.service
# Environment=RSC_HOST_TOKEN=your-token-here
```

Neither unit may be `After=multi-user.target`: both are `WantedBy` it, and
ordering against the same target is a cycle. systemd resolves cycles by
deleting a job, and it will pick the one you wanted.

### Why the ring needs its own root unit

The ring sits on GPIO 12, which is PWM0. Driving SKC6812 timing from PWM0 means
feeding the peripheral by DMA, and that means mapping `/dev/mem`.
`/dev/gpiomem`: the unprivileged path the button, LED and servos use, exposes
only the GPIO register block. No group membership or udev rule avoids root
here. Confining it to a 300-line helper behind a unix socket keeps the daemon
itself unprivileged.

## 8. Power control (optional)

Lets the console and the CYD's power buttons shut the robot down. Without it
those buttons report why they cannot act.

```bash
sudo tee /etc/polkit-1/rules.d/50-rsc-power.rules > /dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.reboot") &&
        subject.user == "rsc") {
        return polkit.Result.YES;
    }
});
EOF
sudo chmod 0644 /etc/polkit-1/rules.d/50-rsc-power.rules

busctl call org.freedesktop.login1 /org/freedesktop/login1 \
  org.freedesktop.login1.Manager CanPowerOff     # expect: s "yes"
```

**What this grants.** Anyone who can run code as `rsc` may power the machine
down without a password, which includes anyone holding the bearer token on the
LAN. Weigh that against what a token already permits: driving both flippers at
full speed, playing audio, lighting the ring. A graceful shutdown is arguably
the mildest of those, and it is gentler on the SD card than the plug-pulling it
replaces. On a shared machine, or a Pi doing something else important, skip it.

## 9. Confirm

```bash
sudo reboot
```

Then:

```bash
systemctl is-active rsc-ring rsc-host@rsc     # both: active
journalctl -b | grep -i "ordering cycle"      # silent
journalctl -u rsc-host@rsc -b | head -20
```

The startup lines worth reading:

```
gpiozero pin factory: LGPIOFactory
state directory: /var/lib/rsc-host
PiRingBackend using helper at /run/rsc/ring.sock
WM8960 mixer preset applied (13/13 controls)
peripherals ready: backend=pi, ...
host serving on ws://0.0.0.0:8765
```

Then connect the web console and try a flipper, the ring, and
`audio.selftest`. The arcade LED should stay dark after
`sudo systemctl stop rsc-host@rsc`.

## Why not pigpio

A previous investigation concluded that the Pi could not drive the servos, the
ring and I2S audio concurrently, and that an RP2350 co-processor was needed.
That conclusion was wrong.

The cause was `pigpiod` running from a malformed unit: a second `[Service]`
block appended to the file, with `ExecStop=/bin/systemctl kill pigpiod`: which
kills its own calling transaction, exits `255/EXCEPTION`, and leaves DMA
channels and the PWM peripheral unrestored. Everything downstream inherited the
wreckage.

With pigpio masked and everything on lgpio, all peripherals run concurrently,
first time. No co-processor is required.

lgpio has two traps that pigpio did not:

- `gpio_claim_output` must precede `tx_servo`. Omitting it is a **silent
  no-op**: the call succeeds and nothing moves.
- `tx_servo(chip, pin, 0)` raises `bad PWM micros` if no wave has ever started
  on that pin, so ceasing pulses needs a guard.

Both are handled in `rsc_host/hal/pi.py`, and `_ensure_gpiozero_factory()`
refuses to start if it finds pigpio in charge.

## Troubleshooting

**Changes appear not to take effect**: stale bytecode. See section 5.

**`No module named rsc_host`**: run from `~/rsc-host`, or check the unit's
`WorkingDirectory`.

**Servos do nothing, but commands succeed**: the `gpio_claim_output` trap.
Check for `PiServoBackend started (pins=...)` in the log.

**Ring unavailable**: `systemctl status rsc-ring`, and confirm the socket
exists: `ls -l /run/rsc/ring.sock` (root:gpio, 0660). Your user must be in
`gpio`.

**Arcade LED lights on shutdown**: releasing a chardev line reverts the pin to
input, floating Q1's gate. `ExecStopPost=` runs `pinctrl set 24 op dl`, which
writes the pad registers directly and therefore sticks. Check `which pinctrl`.
The durable fix is a hardware pull-down on the gate; nothing here survives
SIGKILL.

**Audio is quiet or silent**: run `audio.selftest` from the console and read
the levels rather than guessing. Speech at a normal distance lands near
−30 dBFS RMS with peaks around −10. Below −60 means the mic is not hearing you,
which is the mixer. `audio.mixer.reset` restores the measured recipe.

**No LAN discovery**: `zeroconf` missing, or the network was not up when the
daemon started. Clients can still connect by hostname; mDNS hostname
resolution is independent of our service advertisement.

**No `/dev/serial0`**: section 2 was skipped, or the reboot did not happen.