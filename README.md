# host — Robot Study Companion host service

Bridges on-chassis peripherals (servos, NeoPixel ring, arcade button, audio, front-panel CYD) to LAN clients over an authenticated WebSocket.

Runs on the Raspberry Pi 4 inside the RSC chassis; developable on any laptop via a fake hardware backend.

---

## Status

**Layer 3 of greenfield scaffolding.** Contract (Layer 1), HAL + fakes (Layer 2), and now the network layer (Layer 3): event bus, bearer-token auth, WebSocket server, entry point. The daemon runs. Peripherals wrapping the HAL, the CYD bridge, and the real Pi backend land next.

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

## Run the daemon

```bash
# Laptop dev — fake HAL, no TLS, listens on localhost.
export RSC_HOST_TOKEN=dev
python -m rsc_host
# or, after `pip install -e .`:  rsc-host
```

Connect a client:

```bash
# npm install -g wscat
wscat -c ws://127.0.0.1:8765 -s bearer -s dev
> {"type":"cmd","id":"1","verb":"ping","args":{}}
< {"type":"ack","id":"1","ok":true,"result":{"pong":true}}
> {"type":"cmd","id":"2","verb":"status","args":{}}
< {"type":"ack","id":"2","ok":true,"result":{"version":"0.0.1","verbs":["ping","status"],"subscribers":1}}
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `RSC_HOST_TOKEN` | — (required) | Bearer token for handshake auth |
| `RSC_HOST_BIND` | `127.0.0.1` | Interface to bind |
| `RSC_HOST_PORT` | `8765` | TCP port |
| `RSC_HOST_BACKEND` | `fake` | `fake` or `pi` |
| `RSC_HOST_TLS_CERT` | — | PEM cert path; enables TLS with `_TLS_KEY` |
| `RSC_HOST_TLS_KEY` | — | PEM key path; enables TLS with `_TLS_CERT` |
| `RSC_HOST_LOG_LEVEL` | `INFO` | Python log level |

## Layout

```
rsc_host/
  __main__.py        Entry point: config, signal handling, server lifecycle
  protocol.py        Wire schemas: Cmd, Ack, Event, ErrorCode
  dispatch.py        Verb registry, @verb decorator, async dispatch
  events.py          EventBus: async fan-out to subscribed clients
  auth.py            TokenAuth: bearer-token check via subprotocols
  config.py          Env-var driven Config dataclass
  server.py          WebSocket server (`websockets` library)
  hal/
    types.py         Shared HAL data types (Colour, Edge, GpioEdge)
    base.py          Abstract peripheral bases (Servo / Ring / Gpio* / Serial / Audio)
    fake.py          In-memory fakes with test hooks

tests/
  test_protocol.py   Schema round-trip + validation
  test_dispatch.py   Registration, dispatch, exception isolation
  test_events.py     Bus fan-out, non-blocking publish, subscription lifecycle
  test_auth.py       Token validation, subprotocol extraction
  test_config.py     Env loading, defaults, validation
  test_server.py     End-to-end WS integration (auth, dispatch, events)
  test_hal_types.py  Colour validation, Edge enum
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
