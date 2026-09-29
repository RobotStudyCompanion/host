# Documentation

| File | Covers |
|---|---|
| `pi-setup.md` | Fresh Pi to running service |
| `configuration.md` | Every environment variable, and the three-layer config model |
| `wire-protocol.md` | All 38 verbs, event topics, binary endpoints |
| `cyd_state_note.md` | Front-panel state sync, analysis, plus the firmware changes needed |
| `notes_on_audio.md` | Settling trim, crest factor, and the converter whine |

## What changed in this pass

**`pi-setup.md` was actively harmful and has been rewritten.** It instructed
the reader to install and enable `pigpiod`: the exact thing that caused the
timing failures once blamed on the hardware, and which led to a recommendation
to add an RP2350 co-processor. It also described a PortAudio-based audio path
that no longer exists, a `.venv` layout that conflicts with the working one,
and a `rsc-host-audio-check` tool that does not.

Anyone following the old version on a fresh Pi would have reproduced the
original fault from scratch.

**`wire-protocol.md` documented 12 verbs of 38.** Everything added during the
port was missing: the whole capture-tuning and mixer surface, volume and mute,
servo calibration and hold, ring status, power control, `peripherals.status`.
It also stated `/audio/in` defaults to 16 kHz, which is wrong on current
robots, and described a JSON format preamble on that socket that was removed.

**`configuration.md` is new.** 51 environment variables existed with no
reference at all.

## Still to write

- **The scenario-driven design note** lives outside this repo at present and
  should probably sit alongside these, since it is the method the rest of the
  decisions were made with.
- **A client guide.** The wire protocol says what the daemon accepts, not how
  to build something sensible against it, reconnect handling, which topics to
  track for state, when to poll `peripherals.status`.
- **The hardware handover notes** are not in this directory and are stale in
  one important way: they recommend an RP2350 on timing grounds, which is a conclusion
  this work disproved. The measurements in them remain good; the conclusion
  does not.

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