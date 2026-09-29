# rsc-host wire protocol

The daemon speaks JSON over WebSocket. This is the source-of-truth reference
for anyone building a client, Python, TypeScript, browser JS, or otherwise.

## Endpoints

Three WebSocket paths on one port (default `8765`):

| Path | Direction | Payload | Purpose |
|---|---|---|---|
| `/` | duplex | JSON text frames | Control channel, commands, acks, events |
| `/audio/out` | client → daemon | binary frames | WAV upload for playback |
| `/audio/in` | daemon → client | binary frames | Raw PCM from the capture session |

All three require authentication.

## Authentication

Bearer token via subprotocol negotiation:

```
Sec-WebSocket-Protocol: bearer, <token>
```

A failed handshake returns HTTP `401` before the upgrade completes; a
successful one echoes `bearer` as the negotiated subprotocol.

```js
new WebSocket("ws://rsc-shiny.local:8765/", ["bearer", "dev"]);
```

```python
async with websockets.connect(
    "ws://rsc-shiny.local:8765/",
    subprotocols=["bearer", "dev"],
) as ws:
    ...
```

The token is the only access control. It grants full command of the robot:
flippers at full speed, audio, ring, and, where the polkit rule is installed -
shutdown. Treat it accordingly.

## Discovery

Advertised over mDNS as `_rsc-host._tcp.local`, instance name = hostname. TXT
records: `robot_name`, `version`, `tls` (`"true"`/`"false"`), `proto`
(`"ws"`/`"wss"`).

Requires `zeroconf` on the daemon side. When it is absent, or the network was
not up at start, the service is not advertised. But `<hostname>.local` still
resolves, since that is avahi rather than us.

## Control channel

Three message types, text JSON, one per frame.

### Cmd (client → daemon)

```json
{"type": "cmd", "id": "abc123", "verb": "flipper.left",
 "args": {"speed": 0.5, "ramp_ms": 500}}
```

`id` is a client-chosen correlation string, echoed in the reply. `args` may be
omitted when empty.

### Ack (daemon → client)

```json
{"type": "ack", "id": "abc123", "ok": true,
 "result": {"id": "left", "speed": 0.5, "enabled": true}}
```

```json
{"type": "ack", "id": "abc123", "ok": false,
 "code": "INVALID_ARGS", "message": "speed must be in [-1.0, 1.0], got 2.0"}
```

### Event (daemon → client, broadcast)

```json
{"type": "event", "topic": "button.press", "source": "host",
 "data": {"pin": 23}, "seq": 42}
```

`source` is `"host"` (daemon-emitted) or `"cyd"` (ingested from the panel).
`seq` is monotonic, assigned at publish time, useful for reconnect recovery.

## Error codes

| Code | Meaning |
|---|---|
| `INVALID_MESSAGE` | Top-level message failed schema validation |
| `UNKNOWN_VERB` | Verb not registered on this daemon |
| `INVALID_ARGS` | Args failed validation |
| `PERIPHERAL_BUSY` | In use, cannot accept overlap |
| `PERIPHERAL_UNAVAILABLE` | Hardware absent, driver missing, helper down |
| `UNAUTHENTICATED` | No valid token |
| `RATE_LIMITED` | Per-verb rate limit exceeded |
| `INTERNAL_ERROR` | Unexpected exception; see host logs |

Unknown codes should be treated as `INTERNAL_ERROR` for forward compatibility.

---

## Verbs

### Baseline

| Verb | Args | Result |
|---|---|---|
| `ping` |, | `{"pong": true}` |
| `status` |, | `{"version", "verbs", "subscribers"}` |
| `events.history` | `{"since_seq"?, "limit"?, "topic"?}` | `{"events": [...], "latest_seq"}` |
| `peripherals.status` |, | Full snapshot; see below |

`peripherals.status` is the one to poll for a readiness display:

```json
{
  "backend": "pi",
  "flippers": {"left": {"speed": 0.0, "enabled": true}, ...},
  "servo_calibration": {"left": {"null_us": 1495, "span_us": 100, ...}, ...},
  "ring": {"available": true, "mode": "helper", "current_mode": "off", ...},
  "button_led": {"mode": "off"},
  "state": {"available": true, "directory": "/var/lib/rsc-host", "keys": ["servo"]},
  "cyd": {"available": true, "state_push": false},
  "audio": {"capturing": false, "playing": false, "capture": {...}},
  "pinout": {...}
}
```

`backend` distinguishes a real robot from `"fake"`. `ring.available` and
`cyd.available` tell you which peripherals are actually present.
`state.available` false means settings will not survive a restart.

### Flippers

| Verb | Args | Result |
|---|---|---|
| `flipper.left` / `.right` / `.m3` | `{"speed": -1..1, "ramp_ms"?: 0..10000}` | `{"id", "speed", "enabled"}` |
| `flipper.<id>.stop` |, | `{"id", "speed": 0.0}` |
| `flipper.stop_all` |, | `{"stopped": ["left", "m3", "right"]}` |

`m3` is soft-disabled by default: commands succeed with `enabled: false` and
the backend is never touched. Its pin collides with the ring.

Speed is normalised, not pulse width. The mapping to microseconds comes from
calibration, below.

### Servo calibration

| Verb | Args | Result |
|---|---|---|
| `servo.calibration` |, | All servos' calibration |
| `servo.calibrate` | `{"id", "null_us"?: 500..2500, "span_us"?: 1..800, "invert"?, "persist"?}` | Updated values |
| `servo.hold` | `{"id", "us"?: 500..2500}` | `{"id", "holding_us", "null_us", "offset_us"}` |
| `servo.hold.stop` | `{"id"?}` | `{"released": [...]}` |
| `servo.calibration.store` |, | `{"stored", "path", "calibration"}` |
| `servo.calibration.reset` |, | `{"reset", "overlay_removed", "calibration"}` |

`null_us` is where the servo is genuinely still. `span_us` is the deflection
meaning full speed, and is where a speed mismatch between two physical servos
gets corrected. **Different quantities**: changing one does not imply the
other.

`servo.hold` exists because a stopped servo is not pulsed at all: at speed zero
the backend holds neutral for 120 ms then ceases, since pulsing at neutral adds
roughly 17 dB above 8 kHz to the microphone. Finding a null needs continuous
pulsing at a candidate width, which is what hold provides. Any movement command
or `flipper.stop_all` drops the hold.

Changes are in-memory until stored. Stored calibration is written as a **diff
from the shipped values**, so an untouched field keeps tracking the default
when a later release improves it.

### Ring

| Verb | Args | Result |
|---|---|---|
| `ring.mode` | `{"mode", "params"?}` | `{"mode", "params"}` |
| `ring.off` |, | `{"mode": "off"}` |
| `ring.modes` |, | `{"modes": [...]}` |
| `ring.status` |, | Availability, socket, pixel count, brightness |

Modes: `off`, `solid`, `pulse`, `spin`, `sweep`. Params: `r`, `g`, `b` (0–255)
and `period_ms`.

Returns `PERIPHERAL_UNAVAILABLE` when the root helper is not running.

### Arcade button LED

| Verb | Args | Result |
|---|---|---|
| `button_led` | `{"mode", "params"?: {"duty"?, "period_ms"?}}` | `{"mode"}` |

Modes: `off`, `on`, `pulse`, `breathe`.

### CYD front panel

| Verb | Args | Result |
|---|---|---|
| `cyd.mood` / `theme` / `bright` / `eye_colour` / `bg_colour` / `led` / `blink` / `splash` / `face` / `look` / `mood_cycle` | `{"value"?: string}` | `{"verb", "value"}` |
| `cyd.raw` | `{"line": string}` | `{"line"}` |

`cyd.raw` is the escape hatch, sent verbatim over UART.

All return `PERIPHERAL_UNAVAILABLE` without `pyserial-asyncio` or a panel.

### Audio, playback and capture

| Verb | Args | Result |
|---|---|---|
| `audio.play_url` | `{"url", "preempt"?}` | `{"bytes"}` |
| `audio.stop_play` |, | `{}` |
| `audio.capture.stop` |, | `{}` |
| `audio.devices` |, | Card list and PCM names |
| `audio.selftest` | `{"seconds"?: 0.5..30, "playback"?, "path"?, "normalise"?, "target_dbfs"?: -40..0, "settle_ms"?: 0..2000}` | See below |

`audio.selftest` records, measures, writes a WAV, and optionally plays it back:

```json
{"path": "/tmp/rsc_selftest.wav", "bytes": 287040, "seconds": 2.99,
 "settle_ms_discarded": 250, "format": {...}, "played": true,
 "playback_gain_db": 10.9, "peak_dbfs": -13.9, "rms_dbfs": -30.3,
 "crest_db": 16.4, "clipped_samples": 0}
```

Two details that matter for interpreting it. The first `settle_ms` is discarded
before measuring, because the ADC emits a settling transient that otherwise sets
`peak_dbfs` and reads as clipping. And playback is normalised while the file on
disk stays raw, so `playback_gain_db` tells you how much louder you heard it
than it was recorded.

Healthy speech: RMS near −30 dBFS, crest 12–18 dB, `clipped_samples` zero.

### Audio, capture chain

| Verb | Args | Result |
|---|---|---|
| `audio.capture.config` |, | Current chain settings |
| `audio.capture.tune` | `{"channel_mode"?, "dc_block"?, "stream_rate"?: 8000..48000, "hpf_hz"?: 0..1000, "hpf_mode"?, "gain_db"?: -40..40, "aec"?, "aec_tail_ms"?, "aec_delay_ms"?}` | New config |
| `audio.capture.stats` |, | Frame counts, over/underruns |
| `audio.capture.stats.reset` |, | `{}` |

`channel_mode`: `left`, `right`, `mono`. `hpf_mode`: `movavg`, `butter`, `off`.
`aec`: `off`, `speex`, `webrtc`: scaffolded, not implemented.

The device always runs at 48 kHz stereo; `stream_rate` is reached by integer
decimation, and non-dividing rates are rejected with the list of valid ones.
Device rate, channel count and frame length are deliberately absent: they need
the ALSA device reopened, so they live in the unit file.

### Audio, mixer

| Verb | Args | Result |
|---|---|---|
| `audio.mixer.get` | `{"names"?: [...]}` | `{"card", "controls": {...}}` |
| `audio.mixer.set` | `{"name", "value"}` | `{"control", "value", "pending", "stored", "hint"}` |
| `audio.mixer.preset` |, | Per-control ok/skip |
| `audio.mixer.store` |, | `{"stored", "path", "controls"}` |
| `audio.mixer.reset` |, | `{"reset", "overlay_removed", "preset_controls"}` |

Changes are live but forgotten on restart until stored. `store` writes only the
controls changed this session, as a diff from the shipped preset. `reset`
deletes the overlay and reapplies the preset.

Gain belongs in the analogue boost ahead of the ADC rather than the digital
`Capture` behind it: boost lifts signal relative to the noise floor, `Capture`
lifts both equally.

### Audio, volume and mute

| Verb | Args | Result |
|---|---|---|
| `audio.volume` | `{"percent": 0..100, "persist"?}` | `{"volume", "index", "muted", "controls", "persisted"}` |
| `audio.volume.get` |, | `{"volume", "muted", "index"}` |
| `audio.mute` | `{"muted": bool}` | `{"muted", "volume"}` |
| `audio.mic_mute` | `{"muted": bool}` | `{"mic_muted", "controls"}` |

Volume attenuates the analogue output (`Speaker`, `Headphone`), leaving the DAC
at full scale, so 100% maps to 0 dB rather than the amplifier's ceiling.

Mic mute is **hardware**: it switches off the input boost mixers, so it is not
something a software fault can bypass.

Volume persists on request; **mute never does.** A robot that booted silent
with no visible cause looks broken to anyone without a shell.

### System

| Verb | Args | Result |
|---|---|---|
| `system.power_status` |, | `{"can_power_off", "can_reboot", "allowed", "reason"?, "fix"?}` |
| `system.poweroff` | `{"confirm": true, "delay_s"?: 0..60}` | `{"scheduled", "action", "delay_s"}` |
| `system.reboot` | `{"confirm": true, "delay_s"?: 0..60}` | same |

`confirm` is required and has **no default**: a caller that forgets it gets
`INVALID_ARGS` rather than a powered-off robot.

Before going down the daemon stops the flippers, releases calibration holds,
turns the ring off and notifies the panel. It then answers the caller and
schedules the actual call `delay_s` later (default 2 s), so the reply reaches
the client before the network drops.

Requires the polkit rule. Without it, `allowed` is false and `reason` says why.

---

## Event topics

`source: "host"`:

| Topic | Data |
|---|---|
| `flipper.<id>.state` | `{"speed", "enabled"}` |
| `ring.mode` | `{"mode", "params"}` |
| `button.press` / `button.release` | `{"pin"}` |
| `button_led.state` | `{"mode", "duty"}` |
| `audio.play.started` / `.done` | `{"bytes", "outcome"?}` |
| `audio.stream.started` / `.done` | `{"samplerate", "channels", "sample_width"?, "outcome"?}` |
| `audio.capture.started` | Full capture format |
| `audio.capture.stopped` | `{}` |
| `audio.volume` | `{"volume", "index", "muted", ...}` |
| `audio.mute` | `{"muted", "volume"}` |
| `audio.mic_mute` | `{"mic_muted", "controls"}` |
| `system.poweroff` / `system.reboot` | `{"pending": true}` |

`source: "cyd"`:

| Topic | Data |
|---|---|
| `host_vol` | `{"value": 0..100}` |
| `host_mute` / `host_mic` | `{}` |
| `host_reboot` / `host_poweroff` | `{}` |

Panel actions produce **both**: the `cyd` event recording what arrived, and the
matching `host` event recording what the daemon did about it. A client tracking
state should follow the `host` topics. They are the same names the verbs
publish, whichever surface caused the change.

Ramps emit roughly five `flipper.<id>.state` events regardless of duration, so
a short ramp is still legible rather than arriving as a single jump.

---

## Binary endpoints

### `/audio/out`: upload WAV, daemon plays it

Send WAV bytes as binary frames, split however convenient. The daemon buffers
until it can parse the header, extracts rate, channels and width from `fmt `,
then streams the PCM to ALSA as it arrives. On client close it drains and
closes.

Lifecycle events go to the control channel, not this socket.

RIFF/WAVE PCM (format 1), 16-bit. Non-PCM variants get a `1003` close. Cap:
20 MB per session.

### `/audio/in`: daemon streams captured PCM

Every frame is binary PCM; **nothing else is sent on this socket.** The format
is not inferable from the stream, so it travels on the control channel instead:
`audio.capture.started` carries it, and `audio.capture.config` returns it on
demand.

Defaults are 16-bit signed LE mono at `RSC_HOST_AUDIO_STREAM_RATE`, which on
current robots is 48000. Do not assume 16 kHz.

Ends when the client closes, when the daemon stops capture, or on a zero-length
frame used as an end-of-stream sentinel. A second concurrent client gets a
`1013` close, one capture session at a time.

---

## Reconnect and history replay

```json
{"type": "cmd", "id": "r1", "verb": "events.history",
 "args": {"since_seq": 42, "topic": "button.press"}}
```

Returns events with `seq >= 42`, oldest first, plus `latest_seq`. The buffer is
bounded (256 events), so a long disconnect loses data. Record the last-seen
`seq` before disconnecting if replay must be lossless.