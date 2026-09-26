# Two surfaces, one truth: CYD and console state

A scenario-driven analysis of front-panel state synchronisation, plus the
firmware changes it implies. The host side is built; the CYD side is specified
here for whoever picks it up.

---

## The problem, precisely

Volume, mute and microphone mute are controllable from two places: the CYD
front panel and the web console. Each keeps its own idea of the current state,
and the two drift.

From `MenuCallbacks.cpp`:

```cpp
static bool _muted = false;
static bool _micMuted = false;
static int16_t _volumeLevel = 50;
...
case E_ELEM_IMAGEBTN_VOLUME:
    _muted = !_muted;
    Serial.println("host_mute");
    refreshVolumeIcon();
```

The panel flips its own flag and tells the host to toggle. It never learns what
the host actually did, and the host never learns the panel's flag exists. Note
this contradicts the README, which states the CYD does not track mute state —
it does, in two static booleans.

Three ways that goes wrong:

1. Mute from the console. The panel's `_muted` does not move, so its icon still
   shows unmuted. Press the panel icon and it flips to muted while sending
   `host_mute`, which the host reads as "toggle" and *unmutes*. Icon says
   muted, robot is loud.
2. `_volumeLevel` starts at 50 regardless of the slider's real position, so the
   loud/low icon can be wrong before anyone touches anything.
3. The host restores a persisted volume at boot. The panel knows nothing of it.

---

## Personas and reach

| Persona | Reach | Can they detect a disagreement? |
|---|---|---|
| **Researcher** | Panel, console | Only by ear. No way to tell which surface is lying |
| **Developer** | Both, plus SSH and `amixer` | Yes — `amixer sget Speaker` settles it |
| **Maintainer** | All of the above, plus firmware | Yes |

The uniform-column test does not fire here: the developer and maintainer can
diagnose it, the researcher cannot. That asymmetry is the finding. A developer
testing this would notice the icon is wrong, shrug, and move on, because they
have a way to check. The researcher has two confident displays and no
tiebreaker, which is worse than having one display.

---

## Options

**Scenario.** A researcher mutes at the panel to take a call. A colleague at
the console, seeing the robot is silent, presses unmute. What does each surface
show, and what does the robot do?

| Option | Panel shows | Console shows | Robot |
|---|---|---|---|
| 1. Leave as is | Muted (wrong) | Unmuted | Unmuted |
| 2. Panel authoritative, host follows | Muted | Unmuted, then corrected | Muted |
| 3. Host authoritative, panel follows | Corrected to unmuted | Unmuted | Unmuted |
| 4. Disable console audio controls | Muted | No controls | Muted |
| 5. Last writer wins, both push | Unmuted | Unmuted | Unmuted |

Options 3 and 5 converge in practice: the host owns the mixer, so it is the
only place state genuinely lives, and "last writer wins with the host as
authority" is option 3 with the panel allowed to write first.

**Option 4 deserves more than dismissal.** One surface is better than two
disagreeing ones, and it needs no firmware change. It fails because the console
is the *only* surface for a remote researcher, and because the console is where
tuning happens — removing its audio controls to fix an icon would be the tail
wagging the dog.

**Option 2 is wrong** for a reason worth recording: the panel cannot be
authoritative about a mixer it cannot read. It would be asserting a state it
has no way to verify.

**Chosen: option 3.** The host owns the mixer, so the host owns the truth, and
it pushes after every change from either surface.

One consequence to accept honestly: the panel can change under someone's hand.
A researcher watching the icon flip because a colleague acted remotely will
find that surprising. The alternative is an icon that is quietly wrong, which
is worse — surprise is recoverable, silent inconsistency is not.

---

## Host side (built)

`CydBridge` gains a capability probe and a push method.

**The probe.** At start the bridge sends `vol?`. Current firmware answers
`ERR: unknown command 'vol'`; patched firmware answers `vol: NN`. The bridge
enables pushing only on the latter, so the same daemon works before and after
the firmware lands with nothing to configure and nothing to remember. A panel
that does not answer at all times out and is treated as unable.

**The push.** After every volume, mute or mic change — from the console *or*
from the panel — the host sends the resulting state. Pushing after a
panel-originated press is the important half: that is when the panel's own flag
may have diverged.

Push is silent when unsupported. It fires on every volume change, and a warning
per message would bury the journal under a condition already reported once at
startup and visible in `peripherals.status` as `cyd.state_push`.

---

## CYD firmware changes (for later)

Three commands, one bug fix. All in the existing dispatch-table pattern.

### 1. `vol:NN` — set slider position without emitting

```cpp
static void cmdSetVol(const String &val) {
    int v = val.toInt();
    if (v < 0 || v > 100) { Serial.println("ERR: 0-100"); return; }
    _volumeLevel = v;
    // Inverted to match the 180-degree enclosure mount, same as the slider.
    gslc_ElemXSliderSetPos(&m_gui, m_pElemSlider2, 100 - v);
    refreshVolumeIcon();
    Serial.println("OK");
}
static void cmdGetVol() { Serial.printf("vol:            %d\n", _volumeLevel); }
```

**Critical:** this must not emit `host_vol`. If `gslc_ElemXSliderSetPos` fires
`CbSlidePos`, host and panel will chase each other around a loop. Either the
GUIslice call does not invoke the callback on a programmatic set — verify, do
not assume — or guard it:

```cpp
static bool _suppressVolEmit = false;
// in CbSlidePos, case E_ELEM_SLIDER2:
if (_suppressVolEmit) { _volumeLevel = v; refreshVolumeIcon(); break; }
```

The getter is what the host probes for, so it must exist even if nothing else
reads it.

### 2. `mute:on|off` and `mic:on|off` — set state, not toggle

```cpp
static void cmdSetMute(const String &val) {
    bool b;
    if (!parseBool(val, b)) { Serial.println("ERR: bad bool"); return; }
    _muted = b; refreshVolumeIcon(); Serial.println("OK");
}
static void cmdGetMute() { Serial.printf("mute:           %s\n", _muted ? "on" : "off"); }

static void cmdSetMic(const String &val) {
    bool b;
    if (!parseBool(val, b)) { Serial.println("ERR: bad bool"); return; }
    _micMuted = b;
    applyIcon(_refMic, _micMuted ? mute_on_icon_40x40px : mic_on_icon_40x40px);
    Serial.println("OK");
}
static void cmdGetMic() { Serial.printf("mic:            %s\n", _micMuted ? "on" : "off"); }
```

Absolute, not toggling. The host sends what it decided; the panel does not
interpret.

These need `_muted`, `_micMuted`, `_volumeLevel` and `refreshVolumeIcon()`
reachable from `Config.cpp` — currently `static` in `MenuCallbacks.cpp`. Either
move the setters into that file and declare them in its header, or drop the
`static`.

### 3. Dispatch table entries

```cpp
{"vol",   cmdSetVol,  cmdGetVol,  "0-100 host volume (display only; host owns the mixer)"},
{"mute",  cmdSetMute, cmdGetMute, "on|off host mute state"},
{"mic",   cmdSetMic,  cmdGetMic,  "on|off host mic mute state"},
```

Do **not** add these to NVS. They mirror host state, and restoring a stale
mirror at boot recreates the drift this is meant to fix. The panel should show
nothing definite until the host pushes, which it does at startup.

### 4. The slider drops its final position

Separate from sync, and arguably more serious:

```cpp
static uint32_t _lastVolSendMs = 0;
if (millis() - _lastVolSendMs >= 100) {
    Serial.printf("host_vol:%d\n", v);
    _lastVolSendMs = millis();
}
```

Leading-edge throttle with no trailing send. Drag from 50 to 80 and release
90 ms after the last emission, and the host never hears 80 — it stays at
whatever the last throttled sample was. The slider shows 80, the robot sits at
52, and nothing visibly went wrong.

The brightness slider has the same shape but is harmless: `setBacklight()` runs
on every update and only the serial echo is throttled. For volume the serial
message *is* the action.

Fix by remembering a suppressed value and flushing it:

```cpp
static int16_t _pendingVol = -1;
// in the slider callback, when throttled:
_pendingVol = v;
// in a periodic service function:
void serviceVolumeFlush() {
    if (_pendingVol >= 0 && millis() - _lastVolSendMs >= 100) {
        Serial.printf("host_vol:%d\n", _pendingVol);
        _lastVolSendMs = millis();
        _pendingVol = -1;
    }
}
```

Once `vol:NN` exists, the host's push after a drag also corrects it — but only
to the value the host received, which is the wrong one. The flush is the real
fix; the push cannot substitute for it.

---

## Verifying

Once the firmware lands, the daemon needs no change. On restart the log should
say `CYD state push enabled` and `peripherals.status` should report
`cyd.state_push: true`.

Then the scenario, end to end: mute at the panel, unmute at the console, and
watch the panel icon correct itself.

---

## What the method surfaced

Worth noting for the design-note collection, since this was a different failure
from the mixer case.

**The asymmetry test, not the uniform-column test.** Every option was
diagnosable by a developer and opaque to a researcher. No column was uniform,
so the earlier signature never fired — but the *gap between columns* was the
whole problem. A bug that one persona can trivially check and another cannot is
a bug that will survive testing, because the people testing are the ones who
can check.

**A README can be evidence against itself.** The firmware documentation stated
the CYD does not track mute state. The code tracks it in two static booleans.
Had the analysis stopped at the documented contract, option 1 would have looked
merely incomplete rather than actively wrong, and the drift would have been
designed around instead of fixed.

**Capability probing beats a configuration flag.** A flag needs somebody to
remember to flip it after flashing. Asking the firmware what it supports means
the correct behaviour happens on its own, which matters when the person
flashing and the person configuring are not the same person — or, in the
researcher case, not a person who configures anything at all.