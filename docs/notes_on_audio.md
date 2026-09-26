# Settling trim, crest factor, and what the whine actually is

Two files: `rsc_host/peripherals/registry.py` and `client/console.html`.
Restart the daemon after copying, reload the console page.

## The peak was not your voice

RMS −30.4 with peak −0.1 is a 30 dB crest factor. Speech runs 12–18 dB. A
single spike that far above everything else is the ADC settling transient at
the start of capture, not the recording being too loud — which is exactly why
the legacy test rig discarded the first quarter second:

```python
return left[int(rate * 0.25):]   # drop ADC settling transient
```

`audio.selftest` now does the same. It records `settle_ms` on top of the
requested duration and trims it off the front, so three seconds requested is
still three seconds delivered. Override with `{"settle_ms": 0}` to see the
transient for yourself.

The result gains `crest_db` and `clipped_samples`, and the console uses both:
an actual count of railed samples rather than inferring clipping from peak
alone, and a note when crest is above 24 dB, meaning one spike still dominates.

## The whine is the hardware, and it is in your notes

The earlier measurements found aliased DC-DC converter tones above 6 kHz,
spaced roughly 2250 Hz, recorded as unfixable digitally because the aliasing
has already happened before the ADC — but removed by downsampling to 16 kHz.

At 48 kHz on the wire those tones are fully in band, so you hear them. Nothing
in the capture chain can remove them; only the low-pass ahead of decimation
can, by discarding the band they live in.

That puts the two options in tension, which is worth stating plainly:

| Wire rate | Sounds like | Why |
|---|---|---|
| 48 kHz | correct speech with a high-pitched whine | converter tones in band; no resampling anywhere |
| 16 kHz | no whine, but garbled | decimation removes the tones; something in the decimate-then-resample path is broken |

So 16 kHz is not merely a nicety for speech models. It is also what removes the
whine. Worth fixing properly once the demo is done.

For the fix itself, the `scp` test still decides where to look:

```bash
scp rsc@rsc-shiny.local:/tmp/rsc_selftest.wav .
```

Switch to 16 k first, record, then copy and play it on the laptop. Clean there
means capture is fine and ALSA's playback resampling is the fault; the answer
is to resample deliberately in `dsp.py` before handing bytes to `aplay`.
Garbled there means the file itself is bad and the decimator needs more work.

## Expected after this patch

```
levels: rms -30.4 dBFS, peak -12.6 dBFS, crest 17.8 dB  →  healthy
written to /tmp/rsc_selftest.wav (286080 bytes, 2.98s)
```

Crest in the teens and no clipping line. If the peak is still at the rail with
a high crest after the trim, the spike is something else — a knock on the
chassis, or a genuine transient — and lowering `Capture` is the next move.