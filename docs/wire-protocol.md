# rsc-host wire protocol

The daemon speaks JSON over WebSocket. This is the source-of-truth reference
for anyone building a client — Python, TypeScript, browser JS, or otherwise.

## Endpoints

The daemon exposes three WebSocket paths on the same port (default `8765`):

| Path         | Direction | Payload | Purpose |
|--------------|-----------|---------|---------|
| `/`          | duplex    | JSON text frames | Control channel — commands, acks, events |
| `/audio/out` | client → daemon | binary frames | Streaming WAV upload for playback |
| `/audio/in`  | daemon → client | binary frames | Raw PCM frames from the capture session |

All three require authentication (see below).

## Authentication

Bearer token via WebSocket subprotocol negotiation:

```
Sec-WebSocket-Protocol: bearer, <token>
```

Failed handshake returns HTTP `401 unauthorized` before the WebSocket upgrade
completes. Successful handshake echoes `bearer` as the negotiated subprotocol.

Example (browser):

```js
new WebSocket("ws://shiny.local:8765/", ["bearer", "dev"]);
```

Example (Python `websockets`):

```python
async with websockets.connect(
    "ws://shiny.local:8765/",
    subprotocols=["bearer", "dev"],
) as ws:
    ...
```

## Discovery

The daemon advertises itself via mDNS as `_rsc-host._tcp.local`, service
instance name = robot hostname. TXT records:

- `robot_name` — friendly name (e.g. `Shiny`)
- `version`   — daemon version string
- `tls`       — `"true"` or `"false"`
- `proto`     — `"ws"` or `"wss"`

Reachable at `<hostname>.local:<port>`.

## Control channel (`/`)

Three message types traverse the wire — text JSON, one message per frame.

### Cmd (client → daemon)

```json
{
    "type": "cmd",
    "id":   "abc123",
    "verb": "flipper.left",
    "args": { "speed": 0.5, "ramp_ms": 500 }
}
```

- `id` — client-chosen correlation string; echoed in the reply Ack. Any
  non-empty string. UUIDs, monotonic counters, or plain incrementing integers
  work equally well.
- `verb` — dotted verb name. See [Verbs](#verbs).
- `args` — verb-specific argument object. May be empty.

### Ack (daemon → client, reply to a Cmd)

Success:
```json
{
    "type":   "ack",
    "id":     "abc123",
    "ok":     true,
    "result": { "speed": 0.5, "enabled": true }
}
```

Failure:
```json
{
    "type":    "ack",
    "id":      "abc123",
    "ok":      false,
    "code":    "INVALID_ARGS",
    "message": "speed must be in [-1.0, 1.0], got 2.0"
}
```

- `result` — verb-specific payload on success. May be `null`.
- `code` — stable error code (see [Error codes](#error-codes)) on failure.
- `message` — human-readable detail on failure. Never leaks server internals.

### Event (daemon → client, broadcast)

```json
{
    "type":   "event",
    "topic":  "button.press",
    "source": "host",
    "data":   { "pin": 23 },
    "seq":    42
}
```

- `topic` — dotted event topic (see [Event topics](#event-topics)).
- `source` — `"host"` (daemon-emitted) or `"cyd"` (ingested from front-panel UART).
- `data` — topic-specific payload. May be empty.
- `seq` — monotonic sequence number assigned by the daemon at publish time.
  Useful for reconnect recovery.

## Error codes

Stable enum. New codes append; existing codes never change semantics. Unknown
codes should be treated as `INTERNAL_ERROR` for forward compatibility.

| Code | Meaning |
|------|---------|
| `INVALID_MESSAGE` | Top-level message failed schema validation (bad JSON, wrong shape) |
| `UNKNOWN_VERB` | Verb is not registered on this daemon |
| `INVALID_ARGS` | Args failed validation against the verb's argument schema |
| `PERIPHERAL_BUSY` | Peripheral is in use and can't accept overlap |
| `PERIPHERAL_UNAVAILABLE` | Peripheral is unreachable (hardware absent, driver not loaded) |
| `UNAUTHENTICATED` | Connection lacks a valid auth token |
| `RATE_LIMITED` | Caller exceeded a per-verb rate limit |
| `INTERNAL_ERROR` | Unexpected exception in the handler; see host logs |

## Verbs

### Baseline

| Verb | Args | Result | Notes |
|------|------|--------|-------|
| `ping` | — | `{ "pong": true }` | Liveness |
| `status` | — | `{ "version", "verbs", "subscribers" }` | Introspection |
| `events.history` | `{ "since_seq"?, "limit"?, "topic"? }` | `{ "events": [...], "latest_seq" }` | Replay recent events |

### Flippers

| Verb | Args | Result |
|------|------|--------|
| `flipper.left` | `{ "speed": -1..1, "ramp_ms"?: int }` | `{ "id", "speed", "enabled" }` |
| `flipper.right` | same | same |
| `flipper.m3` | same | same (currently soft-disabled) |
| `flipper.<id>.stop` | — | `{ "id", "speed": 0.0 }` |

### LED ring

| Verb | Args | Result |
|------|------|--------|
| `ring.mode` | `{ "mode": "off"\|"solid"\|"pulse"\|"spin"\|"sweep", "params"?: {...} }` | `{ "mode", "params" }` |
| `ring.modes` | — | `{ "modes": ["off", ...] }` |

`params` for modes (all optional):
- `r`, `g`, `b` — 0..255 colour channels
- `period_ms` — pulse period / rotation period in ms

### Arcade button LED

| Verb | Args | Result |
|------|------|--------|
| `button_led` | `{ "mode": "off"\|"on"\|"pulse"\|"breathe", "params"?: {"duty"?, "period_ms"?} }` | `{ "mode" }` |

### CYD front-panel

| Verb | Args | Result |
|------|------|--------|
| `cyd.mood` / `cyd.theme` / `cyd.bright` / `cyd.eye_colour` / `cyd.bg_colour` / `cyd.led` / `cyd.blink` / `cyd.splash` / `cyd.face` / `cyd.look` / `cyd.mood_cycle` | `{ "value"?: string }` | `{ "verb", "value" }` |
| `cyd.raw` | `{ "line": string }` | `{ "line" }` — escape hatch, sent verbatim over UART |

### Audio

| Verb | Args | Result |
|------|------|--------|
| `audio.play_url` | `{ "url": string, "preempt"?: bool }` | `{ "bytes" }` |
| `audio.stop_play` | — | `{}` |
| `audio.capture.stop` | — | `{}` |
| `audio.devices` | — | `{ "input": [...], "output": [...], "default_input", "default_output" }` |

Streaming playback and capture use the binary endpoints described below.

## Event topics

`source: "host"` events (emitted by the daemon):

| Topic | Data |
|-------|------|
| `flipper.<id>.state` | `{ "speed", "enabled" }` |
| `ring.mode` | `{ "mode", "params" }` |
| `button.press` / `button.release` | `{ "pin" }` |
| `button_led.state` | `{ "mode", "duty" }` |
| `audio.play.started` / `audio.play.done` | `{ "bytes", "outcome"? }` |
| `audio.stream.started` / `audio.stream.done` | `{ "samplerate", "channels", "sample_width"?, "outcome"? }` |
| `audio.capture.started` / `audio.capture.stopped` | `{}` |

`source: "cyd"` events (ingested from the front-panel UART):

| Topic | Data |
|-------|------|
| `host_vol` | `{ "value": int }` |
| `host_mute` / `host_mic` | `{}` |
| `host_reboot` / `host_poweroff` | `{}` |

## Binary endpoints

### `/audio/out` — client uploads WAV, daemon streams to speaker

Client opens the WebSocket to `/audio/out` with the same bearer subprotocol.
Client sends WAV bytes as binary frames — split across as many frames as
convenient. Daemon:

1. Buffers the leading bytes until it can parse the WAV header (usually the
   first frame).
2. Extracts sample rate, channel count, sample width from `fmt ` chunk.
3. Opens an ALSA output stream and starts feeding subsequent PCM bytes to it
   as they arrive — playback begins within tens of ms.
4. On client close, drains remaining audio and closes the connection.

Playback lifecycle events (`audio.stream.started`, `audio.stream.done`) are
broadcast on the control channel — not returned on this socket.

Supported: RIFF/WAVE PCM (format code 1), any channel count, 16-bit samples.
Non-PCM WAV variants return a `1003 unsupported data` close.

Upload cap: 20 MB per session.

### `/audio/in` — daemon streams captured PCM to client

Client opens the WebSocket to `/audio/in`. Daemon starts an ALSA capture
session on connect. Frames arrive as binary WebSocket frames — raw PCM,
matching the daemon's capture format (default 16-bit signed LE, mono, 16 kHz;
configurable via `RSC_HOST_AUDIO_*` env vars).

Session ends when:

- The client closes the WebSocket (daemon stops capture).
- The daemon stops capture for another reason (e.g. shutdown) — WebSocket
  closed by daemon.
- The daemon sends a zero-length binary frame as an end-of-stream sentinel
  (edge case; clients should also handle regular close events).

Second concurrent client gets a `1013 try again later` close — only one
capture session can run at a time.

## Reconnect and history replay

When a client disconnects and reconnects, it can catch up on events that
happened in between by calling `events.history`:

```json
{
    "type": "cmd",
    "id":   "r1",
    "verb": "events.history",
    "args": { "since_seq": 42, "topic": "button.press" }
}
```

Returns events with `seq >= 42`, ordered oldest → newest, plus the current
`latest_seq`. Buffer is bounded (default 256 events) — a long disconnect
may lose data. For lossless replay, the client should record the last-seen
`seq` before disconnecting.
