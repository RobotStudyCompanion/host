# Documentation

| File | Covers |
|---|---|
| [`pi-setup.md`](pi-setup.md) | Fresh Pi to running service |
| [`configuration.md`](configuration.md) | Every environment variable, and the three-layer config model |
| [`wire-protocol.md`](wire-protocol.md) | Every verb, event topic and binary endpoint |
| [`cyd_state_note.md`](cyd_state_note.md) | Front-panel state sync, analysis, plus the firmware changes needed |
| [`notes_on_audio.md`](notes_on_audio.md) | Settling trim, crest factor, and the converter whine |

## Known to be out of date

Checked against the code on 2026-11-14. Fix the doc, or the code, then delete
the line.

- **`configuration.md` gives `RSC_HOST_AUDIO_STREAM_RATE` a default of 48000.**
  The code default is 16000 (`config.py`); 48000 comes from the systemd unit.
  A foreground run without the unit, such as the smoke test in `pi-setup.md`,
  therefore captures at 16 kHz, the path that garbles.
- **`wire-protocol.md` lists `RATE_LIMITED`** as an error code. The enum has it,
  but nothing in the daemon produces it.
- **`notes_on_audio.md` opens as a delivery note** for a patch to
  `registry.py` and `client/console.html`. The console has since moved to its
  own repository.
- **`pi-setup.md` §6 and the top-level README** describe the robot install
  slightly differently. Keep one and link to it from the other.

## Still to write

- **The scenario-driven design note** lives outside this repo at present and
  should probably sit alongside these, since it is the method the rest of the
  decisions were made with.
- **A client guide.** The wire protocol says what the daemon accepts, not how
  to build something sensible against it: reconnect handling, which topics to
  track for state, when to poll `peripherals.status`.


## Still unresolved in the system itself

Listed here because the docs describe them as settled and they are not:

- **16 kHz capture garbles.** 48 kHz is the current default and sounds correct
  apart from a faint converter whine. The `scp` test in `notes_on_audio.md`
  decides whether the fault is the decimator or ALSA's playback resampler.
- **`SERVO_R_NULL` is provisional** and `SERVO_R_SPAN` was set by eye. The
  `servo.hold` workflow can settle the first; the second needs a different
  instrument.
- **AEC is scaffolded, not implemented.** Full duplex needs it.
- **CYD state push is disabled** until the firmware gains `vol`, `mute` and
  `mic` commands. The daemon probes for them at every start and enables itself
  when they appear.