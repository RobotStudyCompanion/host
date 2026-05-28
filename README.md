# host — Robot Study Companion host service

Bridges on-chassis peripherals (servos, NeoPixel ring, arcade button, audio, front-panel CYD) to LAN clients over an authenticated WebSocket.

Runs on the Raspberry Pi 4 inside the RSC chassis; developable on any laptop via a fake hardware backend.

---

## Status

**Layer 2 of greenfield scaffolding.** Contract layer (Layer 1) plus the hardware abstraction layer with a complete fake backend (Layer 2). No server, peripherals, audio routing, or real Pi backend yet. Subsequent layers land incrementally.

---

## Install

```bash
git clone https://github.com/RobotStudyCompanion/host.git
cd host

# Dev setup (laptop or Pi)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Pi-only extras (pigpio, rpi_ws281x) — adds to the above
pip install -e ".[pi,dev]"
```

## Test

```bash
pytest
```

---

## Layout

```
rsc_host/
  protocol.py        Wire-protocol pydantic schemas: Cmd, Ack, Event, ErrorCode
  dispatch.py        Verb registry, @verb decorator, async dispatch coroutine
  hal/
    types.py         Shared HAL data types (Colour, Edge, GpioEdge)
    base.py          Abstract bases per peripheral type (Servo / Ring / Gpio* / Serial / Audio)
    fake.py          In-memory fakes with test hooks (trigger, inject, played, ...)

tests/
  test_protocol.py   Schema round-trip + validation rejection
  test_dispatch.py   Registration rules, dispatch behaviour, exception isolation
  test_hal_types.py  Colour validation, packing, Edge enum
  test_hal_fake.py   Per-fake behaviour and test-hook contracts
```

## Wire protocol (current)

Three message shapes traverse the WebSocket; JSON, one message per frame.

```jsonc
// client → host
{ "type": "cmd", "id": "abc123", "verb": "flipper.left", "args": { "speed": 0.5 } }

// host → client (reply to a Cmd)
{ "type": "ack", "id": "abc123", "ok": true,  "result": { "speed": 0.5 } }
{ "type": "ack", "id": "abc123", "ok": false, "code": "INVALID_ARGS", "message": "..." }

// host → client (broadcast)
{ "type": "event", "topic": "host_vol",           "source": "cyd",  "data": { "value": 42 } }
{ "type": "event", "topic": "flipper.left.state", "source": "host", "data": { "speed": 0.5 } }
```

Verbs are dotted on the host side (`flipper.left`, `cyd.mood`). CYD UART traffic mirrors as events tagged `source: "cyd"`; commands toward the CYD use `cyd.*` verbs and the CYD bridge translates to the underscored serial grammar.

## Licence

Apache-2.0.
