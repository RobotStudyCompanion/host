# Configuration

Everything the daemon reads from its environment. Set these in the systemd
unit, or export them for a foreground run.

Defaults are the values measured on the chassis, so a robot with no
configuration at all should work.

## Layering

Three places a setting can come from, in increasing precedence:

1. **Code defaults**: ship in the image, known-good on first boot
2. **`/var/lib/rsc-host/*.json`**, what a user changed from the console
3. **Environment**: a developer override

A fresh robot has no state files, so layer 1 runs alone. The console writes
layer 2 (`audio.mixer.store`, `servo.calibration.store`); resetting deletes it.
Layer 3 is for things a user has no business changing.

## Server

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_TOKEN` |, | **Required.** Bearer token. No default, deliberately |
| `RSC_HOST_BIND` | `127.0.0.1` | Set `0.0.0.0` on a robot, or nothing on the LAN can reach it |
| `RSC_HOST_PORT` | `8765` | |
| `RSC_HOST_BACKEND` | `fake` | `pi` on a robot |
| `RSC_HOST_LOG_LEVEL` | `INFO` | |
| `RSC_HOST_ADVERTISE` | `true` | mDNS service advertisement |
| `RSC_HOST_ROBOT_NAME` | hostname | Advertised name |
| `RSC_HOST_TLS_CERT` / `_TLS_KEY` |, | Both or neither |
| `RSC_HOST_STATE_DIR` | from systemd | Overrides `StateDirectory=` |

The daemon warns when `ADVERTISE` is on but `BIND` is loopback: mDNS would
publish an address nothing can reach.

## Audio, device

Fixed at start; changing them needs the ALSA device reopened.

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_AUDIO_INPUT` | `plughw:0,0` | ALSA PCM name, not an index |
| `RSC_HOST_AUDIO_OUTPUT` | `plughw:0,0` | |
| `RSC_HOST_AUDIO_DEVICE_RATE` | `48000` | **Do not lower.** Asking ALSA for 16 k directly warbles on this codec |
| `RSC_HOST_AUDIO_DEVICE_CHANNELS` | `2` | |
| `RSC_HOST_AUDIO_FRAME_MS` | `20` | |
| `RSC_HOST_AUDIO_MIXER_CARD` | `0` | |
| `RSC_HOST_AUDIO_APPLY_MIXER` | `true` | Apply the measured preset at start |

## Audio, capture chain

All retunable at runtime via `audio.capture.tune`; these set the boot value.

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_AUDIO_STREAM_RATE` | `48000` | Must divide the device rate |
| `RSC_HOST_AUDIO_CHANNEL_MODE` | `left` | `left`, `right`, `mono` |
| `RSC_HOST_AUDIO_DC_BLOCK` | `true` | Offset measured at +440..900 LSB |
| `RSC_HOST_AUDIO_HPF_HZ` | `80` | Below the speech band |
| `RSC_HOST_AUDIO_HPF_MODE` | `movavg` | `butter` needs scipy |
| `RSC_HOST_AUDIO_GAIN_DB` | `0` | Make-up gain after the filters |
| `RSC_HOST_AUDIO_AEC` | `off` | Scaffolded only |
| `RSC_HOST_AUDIO_AEC_TAIL_MS` | `150` | |
| `RSC_HOST_AUDIO_AEC_DELAY_MS` | `0` | |

**On the stream rate.** 48000 means no resampling anywhere, at the cost of
letting the DC-DC converter's aliased tones (above 6 kHz, spaced ~2250 Hz) sit
in band, audible as a faint whine. 16000 removes them by discarding that band,
and is what speech models expect, but the decimated path currently garbles.
Unresolved; see the audio notes.

Deprecated: `RSC_HOST_AUDIO_SAMPLERATE` sets the stream rate with a warning.
`RSC_HOST_AUDIO_CHANNELS` is ignored; the chain always emits mono.

## Servos

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_SERVO_LEFT_NULL_US` | `1495` | Measured |
| `RSC_HOST_SERVO_RIGHT_NULL_US` | `1510` | **Provisional**: two noisy runs gave 1505 and 1520 |
| `RSC_HOST_SERVO_M3_NULL_US` | `1500` | |
| `RSC_HOST_SERVO_LEFT_SPAN_US` | `100` | |
| `RSC_HOST_SERVO_RIGHT_SPAN_US` | `205` | From `SERVO_R_TRIM=105`, **set by eye, never measured** |
| `RSC_HOST_SERVO_M3_SPAN_US` | `100` | |
| `RSC_HOST_SERVO_*_INVERT` | left/m3 false, right true | Right is mirrored |
| `RSC_HOST_SERVO_MIN_US` / `_MAX_US` | `900` / `2100` | Safety window |
| `RSC_HOST_SERVO_DEADBAND` | `0.02` | Below this, treated as stop |
| `RSC_HOST_SERVO_IDLE_MS` | `120` | Hold neutral, then cease pulses |
| `RSC_HOST_M3_ENABLED` | `false` | Its pin collides with the ring |
| `RSC_HOST_GPIOCHIP` | `0` | |

`IDLE_MS` is load-bearing. Pulsing at neutral adds roughly 17 dB above 8 kHz
and 4–5 dB in the speech band to the microphone: the single worst interferer
measured. Ceasing pulses when idle is what removes it.

Calibration set from the console lands in `/var/lib/rsc-host/servo.json` and
wins over these.

## Ring

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_RING_MODE` | `auto` | `auto`, `helper`, `direct`, `off` |
| `RSC_HOST_RING_SOCKET` | `/run/rsc/ring.sock` | |
| `RSC_HOST_RING_GPIO` | `12` | PWM0. **Not 21**: that is PCM and would kill I2S audio |
| `RSC_HOST_RING_PIXELS` | `16` | |
| `RSC_HOST_RING_BRIGHTNESS` | `0.3` | |
| `RSC_HOST_RING_WHITE_MODE` | `extract` | SKC6812 is RGBW |

`direct` needs the daemon to be root and is not how it is deployed. `helper`
talks to `rsc-ring.service`. `auto` tries the helper and falls back to off.

## Serial (CYD)

| Variable | Default | Notes |
|---|---|---|
| `RSC_HOST_SERIAL_DEVICE` | `/dev/serial0` | |
| `RSC_HOST_SERIAL_BAUD` | `115200` | |
| `RSC_HOST_SERIAL_REQUIRED` | `false` | When false, a missing panel logs and continues |

## A minimal robot unit

```ini
Environment=RSC_HOST_BACKEND=pi
Environment=RSC_HOST_BIND=0.0.0.0
Environment=RSC_HOST_RING_MODE=helper
Environment=RSC_HOST_AUDIO_STREAM_RATE=48000
StateDirectory=rsc-host
```

Plus the token in a drop-in, so reinstalling the unit does not regenerate it.