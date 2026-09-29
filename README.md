# host — Robot Study Companion host service

![status: active development](https://img.shields.io/badge/status-active%20development-orange)
![platform: Raspberry Pi 4](https://img.shields.io/badge/platform-Raspberry%20Pi%204-c51a4a)
![python: 3.13](https://img.shields.io/badge/python-3.13-blue)
![licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-green)

Bridges the peripherals inside an RSC chassis — flipper servos, an addressable
LED ring, an arcade button, audio, and the front-panel display — to clients on
the local network, over an authenticated WebSocket.

Runs on the Raspberry Pi 4 inside the chassis. Developable on any laptop
through a fake hardware backend implementing the same contract, so no robot is
needed to work on the protocol, the peripherals or a client.

RSC is the Robot Study Companion: a stationary desktop robot for research use.
The front panel is a **CYD** ("cheap yellow display"), an ESP32 board with a
touchscreen, running [its own
firmware](https://github.com/RobotStudyCompanion/CYD) and talking to this
daemon over a serial link.

Everything unfinished is listed in [the documentation
index](docs/README.md#still-unresolved-in-the-system-itself) rather than here.

---

## Install

### On a robot

[`docs/pi-setup.md`](docs/pi-setup.md) is the real procedure. Several steps are
outside this repository — systemd units, group membership, an audio
configuration file, and optionally a polkit rule for shutdown — and skipping
them produces a daemon that starts and then cannot reach its hardware.

The Python part:

```bash
python3 -m venv --system-site-packages ~/rsc-env
~/rsc-env/bin/pip install pydantic websockets numpy
# optional, each degrading gracefully if absent:
#   pyserial-asyncio   front panel
#   zeroconf           network discovery
#   scipy              sharper high-pass filter
```

**One virtualenv only.** Do not also `pip install -e .` on the robot. A package
installed into site-packages shadows the source tree, and the daemon will run
the installed copy while you edit the other.

### On a laptop

```bash
git clone https://github.com/RobotStudyCompanion/host.git
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

> The `[dev]` extra is inherited from an earlier revision of this file and has
> not been verified against the current `pyproject.toml`. If it fails, install
> `pydantic websockets numpy pytest pytest-asyncio` directly. Reconciling this
> README with the actual extras is a small open task.

The fake backend needs no hardware and no extras.

## Test

```bash
pytest
```

`tests/test_config.py` currently fails in five places. Four are tests that
predate a deliberate configuration change and need updating; one is a genuine
missing validation. Not yet done.

## Run

```bash
# Laptop: fake hardware, localhost only
RSC_HOST_TOKEN=dev python -m rsc_host

# Robot: real hardware, reachable on the local network
RSC_HOST_BACKEND=pi RSC_HOST_BIND=0.0.0.0 RSC_HOST_TOKEN=dev \
  ~/rsc-env/bin/python -m rsc_host
```

In production it runs as two systemd services: `rsc-host@rsc.service` and
`rsc-ring.service`. Two, because the ring needs root privileges and the rest of
the daemon deliberately does not — see [Architecture](#architecture).

A one-line check with `wscat`:

```bash
wscat -c ws://127.0.0.1:8765 -s bearer -s dev
> {"type":"cmd","id":"1","verb":"ping","args":{}}
< {"type":"ack","id":"1","ok":true,"result":{"pong":true}}
```

For anything more, use the web console:
[RobotStudyCompanion/console](https://github.com/RobotStudyCompanion/console),
published at `rsc.ee/console.html`. It is a single static HTML file and can be
opened from disk against any robot on the same network.

## Configuration

`RSC_HOST_TOKEN` is required and has no default. Everything else does, and the
defaults are values measured on the chassis rather than guessed — the reasoning
and the measurements behind each are recorded in
[`docs/configuration.md`](docs/configuration.md), which covers all 51
variables.

Settings changed from the console are written to `/var/lib/rsc-host/` and layer
over the shipped defaults, so per-robot tuning and the shipped recipe coexist
without either editing the other. A robot with no configuration at all should
work.

## Layout

```
rsc_host/
  __main__.py          Entry point: config, signals, lifecycle
  protocol.py          Wire schemas: Cmd, Ack, Event, ErrorCode
  dispatch.py          Verb registry and async dispatch
  events.py            Event bus: async fan-out to subscribers
  auth.py              Bearer-token check via WebSocket subprotocols
  config.py            Grouped settings read from the environment
  errors.py            Peripheral error types, importable from any layer
  state.py             Durable user settings, in a directory the daemon owns
  power.py             Shutdown and reboot, without root
  server.py            WebSocket server
  audio_endpoints.py   Binary audio handlers
  discovery.py         Network service advertisement
  client.py            Python client library
  ring_helper.py       Root-only LED ring daemon, its own service
  hal/                 Hardware abstraction layer
    types.py           Colour, Edge, GpioEdge
    base.py            Abstract peripheral bases
    fake.py            In-memory fakes with test hooks
    pi.py              Real backend for the Pi
    dsp.py             Capture chain: decimation, filters, echo-cancel hook
  peripherals/         Semantic behaviour composed from the abstraction layer
    registry.py        Wires backends to peripherals to verbs
    flipper.py         Servo wrapper with ramping
    ring.py            Mode machine
    button.py          Edge debouncing
    button_led.py      Brightness modes
    cyd.py             Front-panel serial bridge
    audio.py           Playback and capture sessions

docs/                  Setup, configuration, wire protocol, design notes
systemd/               Service unit files
scripts/               Install helpers
tests/                 Behavioural tests, mostly against the fakes
```

## Architecture

Four layers, each depending only on the one below.

**Contract** — `protocol.py`, `dispatch.py`, `events.py`. Message shapes and
routing. No hardware, no input or output.

**Hardware abstraction** — `hal/`. One abstract class per peripheral *type*,
with the fake and Pi backends implementing all of them. Asynchronous
throughout. Domain logic lives above, so this layer stays a thin translation.

**Peripherals** — `peripherals/`. Behaviour composed from backends: ramping,
debouncing, mode machines, capture sessions.

**Network** — `server.py`, `auth.py`, `audio_endpoints.py`, `discovery.py`.

Three decisions look odd until you know the reason, and all three came from
measurement rather than preference.

**The general-purpose input/output layer uses lgpio, not pigpio.** An earlier
investigation concluded the Pi could not drive servos, the ring and digital
audio concurrently, and recommended adding a co-processor. That was wrong: the
cause was a malformed `pigpiod` service file whose stop command killed its own
transaction, leaving hardware resources unrestored. With pigpio masked and
everything moved to lgpio, all peripherals run together. The full account, and
the two lgpio pitfalls it introduced, are in
[`docs/pi-setup.md`](docs/pi-setup.md#why-not-pigpio). Do not reintroduce
pigpio.

**The LED ring runs in a separate root process.** It sits on a pin whose timing
must be driven by direct memory access, which needs privileges the unprivileged
memory interface does not grant. Rather than run the whole daemon as root, a
small helper owns the strip behind a local socket. The daemon stays
unprivileged and tolerates the helper being absent.

**Audio uses `arecord` and `aplay` rather than a sound library.** PortAudio
cannot address the ALSA `plughw` device and negotiates its own sample rate,
which on this codec produces audible warble. Subprocesses also give full duplex
for free and keep the mixer consistent with what the daemon believes. Measured
levels and the reasoning are in
[`docs/notes_on_audio.md`](docs/notes_on_audio.md).

## Documentation

| Document | Covers |
|---|---|
| [`docs/README.md`](docs/README.md) | Index, and what is still unresolved |
| [`docs/pi-setup.md`](docs/pi-setup.md) | Fresh Pi to running service |
| [`docs/configuration.md`](docs/configuration.md) | Every environment variable |
| [`docs/wire-protocol.md`](docs/wire-protocol.md) | All 38 verbs, events, binary endpoints |
| [`docs/cyd_state_note.md`](docs/cyd_state_note.md) | Front-panel state, and the firmware changes it needs |
| [`docs/notes_on_audio.md`](docs/notes_on_audio.md) | Capture levels and known artefacts |

## Related repositories

- [console](https://github.com/RobotStudyCompanion/console) — the web console
- [CYD](https://github.com/RobotStudyCompanion/CYD) — front-panel firmware
- [robotstudycompanion.github.io](https://github.com/RobotStudyCompanion/robotstudycompanion.github.io)
  — the site, and where the console is published

## Licence

Apache-2.0.