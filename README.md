# host — Robot Study Companion host service

Bridges on-chassis peripherals — flipper servos, NeoPixel ring, arcade button,
audio, and the CYD front panel — to LAN clients over an authenticated
WebSocket.

Runs on the Raspberry Pi 4 inside the RSC chassis. Developable on any laptop
through a fake hardware backend that implements the same contract.

---

## Status

**Running on hardware.** Every peripheral works, individually and
concurrently, and the daemon survives a reboot unattended.

The GPIO layer runs on **lgpio**, not pigpio. An earlier investigation
concluded the Pi could not drive servos, the ring and I2S audio at once and
that an RP2350 co-processor was needed. That was wrong: the cause was a
malformed `pigpiod` unit whose `ExecStop` killed its own transaction and left
DMA channels unrestored. With pigpio masked, everything runs together. **Do not
reintroduce pigpio.**

Known-unfinished, documented rather than hidden:

- 16 kHz capture garbles; 48 kHz is the default and sounds correct apart from a
  faint DC-DC converter whine
- `SERVO_RIGHT_NULL_US` is provisional; `SERVO_RIGHT_SPAN_US` was set by eye
- Acoustic echo cancellation is scaffolded, not implemented
- CYD state push waits on three firmware commands; the daemon probes for them
  at every start and enables itself when they appear

---

## Install

### On a robot

See [`docs/pi-setup.md`](docs/pi-setup.md) — there is more to it than pip, and
some of it (polkit, systemd units, ALSA, group membership) is not in this repo.

The short version:

```bash
python3 -m venv --system-site-packages ~/rsc-env
~/rsc-env/bin/pip install pydantic websockets numpy
# optional, each failing soft: pyserial-asyncio  zeroconf  scipy
```

**One virtualenv only.** Do not also `pip install -e .` — a package in
site-packages shadows the source tree, and the daemon will run the installed
copy while you edit the other.

### On a laptop

```bash
git clone https://github.com/RobotStudyCompanion/host.git
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The fake backend needs no hardware and no extras.

## Test

```bash
pytest
```

`tests/test_config.py` currently disagrees with the config rewrite in five
places — device indices, channel counts, and a negative-rate guard. Four of
those are tests that need updating to match decisions already taken; one is a
real gap. Not yet done.

---

## Run

```bash
# Laptop: fake hardware, localhost only
RSC_HOST_TOKEN=dev python -m rsc_host

# Robot: real hardware, reachable on the LAN
RSC_HOST_BACKEND=pi RSC_HOST_BIND=0.0.0.0 RSC_HOST_TOKEN=dev \
  ~/rsc-env/bin/python -m rsc_host
```

As a service, `rsc-host@rsc.service` plus `rsc-ring.service` — two units,
because the ring needs root and the daemon does not. See
[`docs/pi-setup.md`](docs/pi-setup.md).

Quick check with `wscat`:

```bash
wscat -c ws://127.0.0.1:8765 -s bearer -s dev
> {"type":"cmd","id":"1","verb":"ping","args":{}}
< {"type":"ack","id":"1","ok":true,"result":{"pong":true}}
```

For anything beyond that, use the console —
[RobotStudyCompanion/console](https://github.com/RobotStudyCompanion/console),
served at `rsc.ee/console.html`. It is a single static HTML file; open it
locally against a robot on the same LAN.

## Configuration

`RSC_HOST_TOKEN` is required and has no default. Everything else has one, and
the defaults are the values measured on the chassis — a robot with no
configuration should work.

Full reference: [`docs/configuration.md`](docs/configuration.md) (51 variables).

Settings a user changes from the console are written to
`/var/lib/rsc-host/*.json` and layer over the code defaults, so the shipped
recipe and per-robot tuning coexist without either editing the other.

## Layout

```
rsc_host/
  __main__.py          Entry point: config, signals, lifecycle
  protocol.py          Wire schemas: Cmd, Ack, Event, ErrorCode
  dispatch.py          Verb registry and async dispatch
  events.py            EventBus: async fan-out to subscribers
  auth.py              Bearer-token check via subprotocols
  config.py            Grouped settings from the environment
  errors.py            Peripheral error types, importable from any layer
  state.py             Durable user settings in a directory the daemon owns
  power.py             Shutdown and reboot via logind, without root
  server.py            WebSocket server
  audio_endpoints.py   /audio/in and /audio/out binary handlers
  discovery.py         mDNS advertisement
  client.py            Python client library
  ring_helper.py       Root-only NeoPixel daemon, its own systemd unit
  hal/
    types.py           Colour, Edge, GpioEdge
    base.py            Abstract peripheral bases
    fake.py            In-memory fakes with test hooks
    pi.py              Real backend: lgpio, ALSA subprocesses, ring client
    dsp.py             Capture chain — decimation, filters, AEC hook
  peripherals/
    registry.py        Wires HAL to peripherals to verbs
    flipper.py         Servo wrapper with ramping
    ring.py            Mode machine
    button.py          Edge debouncing
    button_led.py      PWM modes
    cyd.py             Front-panel UART bridge
    audio.py           Playback and capture sessions

docs/                  Setup, configuration, wire protocol, design notes
systemd/               Unit files
scripts/               Install helpers
tests/                 Behavioural tests, mostly against the fakes
```

## Architecture

Four layers, each depending only on the one below.

**Contract** — `protocol.py`, `dispatch.py`, `events.py`. Message shapes and
routing. No hardware, no I/O.

**HAL** — `hal/`. One abstract class per peripheral *type*; `fake` and `pi`
implement all of them. Async throughout. Domain logic lives above this, so the
HAL stays a thin translation to the hardware.

**Peripherals** — `peripherals/`. Semantic behaviour composed from HAL
backends: ramping, debouncing, mode machines, capture sessions.

**Network** — `server.py`, `auth.py`, `audio_endpoints.py`, `discovery.py`.

Two design choices worth knowing before reading the code:

**The ring runs in a separate root process.** GPIO 12 is PWM0, which needs DMA,
which needs `/dev/mem`. Rather than run the whole daemon as root, a ~300-line
helper owns the strip behind a unix socket. The daemon stays unprivileged and
tolerates the helper being absent.

**Audio goes through `arecord` and `aplay`, not PortAudio.** PortAudio cannot
address `plughw` and negotiates its own rate, which on this codec produces
audible warble. Subprocesses also give full duplex for free and keep `amixer`
consistent with what the daemon believes.

## Documentation

| Document | Covers |
|---|---|
| [`docs/pi-setup.md`](docs/pi-setup.md) | Fresh Pi to running service |
| [`docs/configuration.md`](docs/configuration.md) | Every environment variable |
| [`docs/wire-protocol.md`](docs/wire-protocol.md) | All 38 verbs, events, binary endpoints |
| [`docs/cyd_state_note.md`](docs/cyd_state_note.md) | Front-panel state sync, and the firmware changes it needs |
| [`docs/notes_on_audio.md`](docs/notes_on_audio.md) | Capture levels, the settling transient, the whine |

## Related repositories

- [console](https://github.com/RobotStudyCompanion/console) — the web console
- [CYD](https://github.com/RobotStudyCompanion/CYD) — front-panel firmware
- [robotstudycompanion.github.io](https://github.com/RobotStudyCompanion/robotstudycompanion.github.io)
  — the site, and where the console is published

## Licence

Apache-2.0.