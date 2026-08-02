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
    build-essential \
    avahi-daemon
```

Give the Pi a friendly name — this doubles as its network identity and its
advertised name over mDNS.

```bash
sudo hostnamectl set-hostname Shiny        # or Pinky, Minion, rsc-01, ...
sudo systemctl enable --now avahi-daemon
```

After a reboot the Pi is reachable at `Shiny.local` from any machine on the
LAN (Linux, macOS, Windows 10+). The daemon additionally advertises itself
as ``_rsc-host._tcp.local`` for zero-config client discovery.

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

## 5. Get the code onto the Pi (Syncthing)

Two options: **git clone** (simple, one-shot) or **Syncthing** (auto-updates
as you edit on your laptop — worth setting up if you'll iterate).

### Option A — git clone (one-shot)

```bash
cd ~
git clone https://github.com/RobotStudyCompanion/host.git
```

Skip to section 6.

### Option B — Syncthing (auto-sync from laptop)

Install and enable as a user service — runs under your login, not root, so
synced files land with the right ownership.

```bash
sudo apt install -y syncthing
sudo systemctl enable --now syncthing@$USER.service
sudo systemctl status syncthing@$USER.service   # active (running)?
```

Syncthing binds its GUI to `127.0.0.1:8384` — safe, but unreachable from the
laptop directly. Open a tunnel from the laptop:

```bash
# On the laptop, in a spare terminal — leave it running
ssh -L 8384:127.0.0.1:8384 <your-pi-host>
```

Browse to `http://127.0.0.1:8384` on the laptop; you're now looking at the
Pi's Syncthing GUI. It will prompt for an admin username/password on first
load — set it.

Pair the devices:

* On the Pi's GUI: **Actions → Show ID.** Copy the device ID.
* On the laptop (run Syncthing there too if you haven't): same, **Actions →
  Show ID.**
* On the Pi's GUI: **Add Remote Device**, paste the laptop's ID, save.
* On the laptop's GUI: accept the incoming device prompt.

Share the host/ folder:

* On the laptop: **Add Folder.** Path = wherever `host/` lives locally,
  label "host", share with the Pi.
* On the Pi's GUI: accept the shared folder prompt, set path to `/home/$USER/host`.

**Ignore patterns** — set on both sides (Folder → Edit → Ignore Patterns):

```
.venv
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
.stfolder
.stversions
```

The Python virtualenv is platform-specific binaries — syncing it would break
things. The others are caches; syncing them just wastes traffic.

Within 30 s files appear in `/home/$USER/host` on the Pi.

## 6. Install the host package

```bash
cd ~/host
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

### Audio device selection

By default the daemon uses the ALSA default input and output. To pin a
specific device — useful when a USB mic and an audio HAT coexist, or the
HAT itself needs override — list what's available:

```bash
rsc-host-audio-check
```

Sample output:

```
Default input:  0
Default output: 0

idx  in   out  rate     name
------------------------------------------------------------
0    2*   2*   48000    seeed2micvoicec: - (hw:0,0)
1    1    0    44100    USB PnP Sound Device: (hw:1,0)
2    0    2    48000    HDMI 0: (hw:2,0)
```

The `*` marks the current default. To use device 1 for input and 2 for
output, set the env vars (in the shell for a foreground run, or in the
systemd unit for the service):

```bash
export RSC_HOST_AUDIO_INPUT=1
export RSC_HOST_AUDIO_OUTPUT=2
export RSC_HOST_AUDIO_SAMPLERATE=16000
```

Values can be integer indices or name substrings (e.g. ``USB PnP`` matches
device 1 above).

Smoke-test a specific device before pointing the daemon at it:

```bash
rsc-host-audio-check --test-play 2                 # beep on output 2
rsc-host-audio-check --test-record 1 --sec 3       # record 3s from input 1
```

## 7. Install the systemd unit

Use the bundled installer — handles capability grants, unit installation, and
token setup in one go. Run from the repo root with your venv active:

```bash
source .venv/bin/activate
./scripts/install-systemd.sh
```

The script will:

* Grant `cap_sys_rawio` + `cap_dac_override` + `cap_sys_nice` to the venv's
  Python (needed for NeoPixel `/dev/mem` access).
* Install the unit template at `/etc/systemd/system/rsc-host@.service`.
* Prompt for a bearer token (or generate one) and store it in a systemd
  drop-in at `/etc/systemd/system/rsc-host@$USER.service.d/token.conf`
  (chmod 600).
* Enable and start `rsc-host@$USER.service`.
* Print status and useful commands.

Save the token somewhere safe — clients need it to connect.

To edit config after install:

```bash
sudo systemctl edit rsc-host@$USER.service    # add extra Environment= lines
sudo systemctl restart rsc-host@$USER.service
```

Common commands:

```bash
sudo systemctl status  rsc-host@$USER.service
sudo systemctl restart rsc-host@$USER.service
sudo systemctl stop    rsc-host@$USER.service
journalctl -u rsc-host@$USER.service -f       # live logs
```

## 8. Reboot to confirm boot-order behaviour

```bash
sudo reboot
```

After the Pi comes back:

```bash
systemctl status pigpiod rsc-host@pi.service
```

Both should be active. `rsc-host` will have started *after* `pigpiod` per
the `After=` and `Requires=` directives in the unit.

## 9. Safe-restart behaviour

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