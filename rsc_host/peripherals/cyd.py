"""CYD (front-panel display) bridge.

Two-way UART link between the host daemon and the ESP32-driven CYD:

* **Reader task** — consumes newline-delimited lines from the SerialBackend.
  Lines starting with ``host_`` are classified into events tagged
  ``source: cyd`` and published on the bus. Everything else is logged and
  ignored (defensive against firmware chatter we don't yet handle).

* **Writer** — accepts dotted verbs from the daemon side (``cyd.mood``,
  ``cyd.theme``, …), translates to the underscored serial grammar
  (``mood:HAPPY``, ``theme:dark``, …), and writes to the SerialBackend.

* **Screen state** — the face shown, cycling, brightness and auto-brightness
  belong to the screen. The bridge records what the screen last reported (or,
  until it reports, what the host last sent) and publishes ``cyd.state`` when
  any of it changes.

The curated verb map is deliberately explicit — adding a verb is one line.
The ``cyd.raw`` escape hatch lets clients send anything to the CYD without
waiting for a daemon release, at the cost of coupling to the CYD's exact
line format. ``cyd.raw`` is not tracked in the screen state.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, replace

from rsc_host.events import EventBus
from rsc_host.hal.base import SerialBackend
from rsc_host.protocol import Event

log = logging.getLogger(__name__)


# ---- host_* line ingestion ----

# Lines the CYD emits toward the host. Everything else is ignored (with a
# debug log — helpful for spotting protocol drift).
#
# Value: (topic, parser). Parser receives the payload string (after the ':')
# and returns the event data dict, or raises ValueError to reject the line.

#: The faces the firmware knows by name, in its own order (``Config.cpp``).
MOODS: tuple[str, ...] = (
    "NEUTRAL", "HAPPY", "ANGRY", "SAD", "EXCITED",
    "ANNOYED", "QUESTIONING", "IDLE1", "IDLE2", "IDLE3",
)
#: What the screen reports after ``face``, ``face_l`` or ``face_r``.
CUSTOM_FACE = "CUSTOM"


def _parse_int(payload: str) -> dict:
    return {"value": int(payload.strip())}


def _parse_empty(payload: str) -> dict:
    # host_mute, host_mic — no payload expected; ignore any trailing text.
    return {}


def _parse_face(payload: str) -> dict:
    name = payload.strip().upper()
    if name not in MOODS and name != CUSTOM_FACE:
        raise ValueError(f"unknown face {payload.strip()!r}")
    return {"value": name}


def _parse_on_off(payload: str) -> dict:
    word = payload.strip().lower()
    if word not in ("on", "off"):
        raise ValueError(f"expected on or off, got {payload.strip()!r}")
    return {"value": word == "on"}


def _parse_percent(payload: str) -> dict:
    value = int(payload.strip())
    if not 0 <= value <= 100:
        raise ValueError(f"{value} is outside 0-100")
    return {"value": value}


_CYD_EVENT_MAP: dict[str, tuple[str, callable]] = {
    # Requests: the host acts on these (see registry._cyd_control_loop).
    "host_vol":         ("host_vol",         _parse_int),
    "host_mute":        ("host_mute",        _parse_empty),
    "host_mic":         ("host_mic",         _parse_empty),
    "host_reboot":      ("host_reboot",      _parse_empty),
    "host_poweroff":    ("host_poweroff",    _parse_empty),
    # The screen restarted; a full report follows.
    "host_ready":       ("host_ready",       _parse_empty),
    # Reports: they only update the screen state. The host never writes to the
    # screen in reply to one, or the two would chase each other.
    "host_face":        ("host_face",        _parse_face),
    "host_cycle":       ("host_cycle",       _parse_on_off),
    "host_bright":      ("host_bright",      _parse_percent),
    "host_auto_bright": ("host_auto_bright", _parse_on_off),
}

#: Report line -> the screen-state field it sets.
_REPORT_FIELDS: dict[str, str] = {
    "host_face": "mood",
    "host_cycle": "cycle",
    "host_bright": "bright",
    "host_auto_bright": "auto_bright",
}


@dataclass(frozen=True, slots=True)
class ScreenState:
    """What the host knows about the screen. ``None`` means not known.

    ``reports`` is False until the firmware sends a report line or
    ``host_ready``; until then the other fields are only what the host last
    sent, since old firmware never says what it is showing.
    """

    mood: str | None = None
    cycle: bool | None = None
    bright: int | None = None
    auto_bright: bool | None = None
    reports: bool = False


# ---- cyd.* → serial translation ----

# Curated. Extend by adding a row; ``cyd.raw`` in the bridge sends whatever
# ``line`` param the caller supplies.
_CYD_VERB_TO_SERIAL: dict[str, str] = {
    "cyd.mood":       "mood",
    "cyd.theme":      "theme",
    "cyd.bright":     "bright",
    "cyd.eye_colour": "eye_colour",
    "cyd.bg_colour":  "bg_colour",
    "cyd.led":        "led",
    "cyd.blink":      "blink",
    "cyd.splash":     "splash",
    "cyd.face":       "face",
    "cyd.look":       "look",
    "cyd.mood_cycle": "mood_cycle",
}


def curated_cyd_verbs() -> tuple[str, ...]:
    """The dotted verb names the CYD bridge accepts (excluding ``cyd.raw``)."""
    return tuple(sorted(_CYD_VERB_TO_SERIAL.keys()))


_PERCENT = re.compile(r"\d{1,3}")


def _check_mood(value: str | None) -> str:
    name = (value or "").strip().upper()
    if name not in MOODS:
        raise ValueError(f"mood must be one of {', '.join(MOODS)}")
    return name


def _check_on_off(value: str | None) -> str:
    word = (value or "").strip().lower()
    if word not in ("on", "off"):
        raise ValueError("mood_cycle must be on or off")
    return word


def _check_bright(value: str | None) -> str:
    text = (value or "").strip()
    if not _PERCENT.fullmatch(text) or not 1 <= int(text) <= 100:
        raise ValueError("bright must be a whole number from 1 to 100")
    return str(int(text))


# Verbs whose value the host checks before anything goes to the screen. The
# rest pass through as they are; the firmware answers ERR for a bad one.
_VALUE_CHECKS = {
    "cyd.mood": _check_mood,
    "cyd.mood_cycle": _check_on_off,
    "cyd.bright": _check_bright,
}


class CydBridge:
    """Bidirectional CYD link atop a :class:`SerialBackend`."""

    def __init__(self, backend: SerialBackend, bus: EventBus) -> None:
        self._backend = backend
        self._bus = bus
        self._reader_task: asyncio.Task[None] | None = None
        # None = not yet probed, True/False = firmware answered.
        self._supports_push: bool | None = None
        self._probe_reply: asyncio.Future[str] | None = None
        self._screen = ScreenState()

    async def start(self) -> None:
        """Kick off the reader loop. Backend must already be started."""
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="cyd-reader"
        )

    async def stop(self) -> None:
        """Cancel the reader task and wait for it to exit."""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None

    # ---- Reader ----

    async def _reader_loop(self) -> None:
        """Consume lines forever; classify ``host_*`` lines into events."""
        while True:
            try:
                line = await self._backend.read_line()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("CYD serial read failed; retrying")
                await asyncio.sleep(0.5)
                continue
            await self._ingest_line(line)

    async def _ingest_line(self, line: str) -> None:
        """Classify one line. Malformed / unknown lines are logged, not raised."""
        line = line.strip()
        if not line:
            return

        # A probe in flight claims the first line that answers it. Done before
        # the event map so a reply cannot be mistaken for a front-panel action.
        if self._probe_reply is not None and not self._probe_reply.done():
            if line.startswith(("vol:", "ERR")):
                self._probe_reply.set_result(line)
                return

        prefix, _, payload = line.partition(":")
        entry = _CYD_EVENT_MAP.get(prefix)
        if entry is None:
            log.debug("CYD: ignoring unknown line: %r", line)
            return

        topic, parser = entry
        try:
            data = parser(payload)
        except ValueError as exc:
            log.warning("CYD: malformed %s line %r: %s", prefix, line, exc)
            return

        if prefix == "host_ready":
            # Only firmware that can be told about volume sends this, and it
            # may arrive long after a start-up probe that timed out because the
            # screen was unplugged. Switched on before the event goes out, as
            # the control loop pushes the audio state when it sees it.
            if not self._supports_push:
                log.info("CYD state push enabled (screen sent host_ready)")
            self._supports_push = True

        await self._bus.publish(Event(topic=topic, source="cyd", data=data))

        if prefix == "host_ready":
            await self._update_screen(reports=True)
        elif prefix in _REPORT_FIELDS:
            await self._update_screen(
                reports=True, **{_REPORT_FIELDS[prefix]: data["value"]}
            )

    # ---- Screen state ----

    @property
    def reports(self) -> bool:
        """Whether the firmware reports its face, cycling and brightness."""
        return self._screen.reports

    def snapshot(self) -> dict:
        """The screen state as ``cyd.state`` carries it."""
        return asdict(self._screen)

    async def _update_screen(self, **changes) -> None:
        """Record ``changes``; publish ``cyd.state`` only if something moved."""
        updated = replace(self._screen, **changes)
        if updated == self._screen:
            return
        self._screen = updated
        await self._bus.publish(
            Event(topic="cyd.state", source="host", data=self.snapshot())
        )

    # ---- State push ----
    #
    # The panel keeps its own mute flags and flips them on every press, quite
    # independently of what the host decides. Mute from the console and the
    # panel's flag does not move; press the panel icon and it flips to whatever
    # it was not, which may now agree with the host or invert it. Two sources
    # of truth, drifting.
    #
    # The host is the authority — it owns the mixer — so it pushes state after
    # every change and the panel follows. That needs `vol`, `mute` and
    # `mic_mute` commands in the CYD dispatch table, which older firmware
    # lacks. Rather than
    # gate this on a flag somebody has to remember to flip, the bridge asks the
    # firmware once at start and enables itself when the commands appear.

    async def probe_push_support(self, timeout: float = 1.5) -> bool:
        """Ask the firmware whether it can be told about volume state.

        Sends ``vol?``. Current firmware answers ``ERR: unknown command``;
        patched firmware answers ``vol: NN``. Either way the daemon carries on
        — a panel that cannot be updated is the situation we already have.
        """
        loop = asyncio.get_running_loop()
        self._probe_reply = loop.create_future()
        try:
            await self._backend.write_line("vol?")
            reply = await asyncio.wait_for(self._probe_reply, timeout)
        except (asyncio.TimeoutError, Exception) as exc:
            self._supports_push = False
            log.info(
                "CYD state push disabled (no answer to 'vol?': %s). The panel "
                "will show its own guess at volume and mute.",
                type(exc).__name__,
            )
            return False
        finally:
            self._probe_reply = None

        self._supports_push = reply.startswith("vol:")
        if self._supports_push:
            log.info("CYD state push enabled (firmware answered %r)", reply)
        else:
            log.info(
                "CYD state push disabled (firmware lacks 'vol'); panel volume "
                "and mute icons will drift from the host"
            )
        return self._supports_push

    @property
    def supports_push(self) -> bool:
        return bool(self._supports_push)

    async def push_state(
        self,
        *,
        volume: int | None = None,
        muted: bool | None = None,
        mic_muted: bool | None = None,
    ) -> dict:
        """Tell the panel what the host believes. No-op when unsupported.

        Silent by design when the firmware cannot accept it: this fires on
        every volume change, and logging a warning each time would bury the
        journal under a condition the operator already knows about.
        """
        if not self._supports_push:
            return {"pushed": False, "reason": "firmware lacks state commands"}
        sent: list[str] = []
        if volume is not None:
            sent.append(f"vol:{max(0, min(100, int(volume)))}")
        if muted is not None:
            sent.append(f"mute:{'on' if muted else 'off'}")
        if mic_muted is not None:
            sent.append(f"mic_mute:{'on' if mic_muted else 'off'}")
        for line in sent:
            try:
                await self._backend.write_line(line)
            except Exception:
                log.warning("CYD push failed for %r", line)
                return {"pushed": False, "reason": "serial write failed"}
        return {"pushed": True, "sent": sent}

    # ---- Screen on and off ----
    #
    # Firmware that answers the probe has ``screen:on|off``. Older firmware
    # gets the nearest it has: ``resume`` undoes ``pause``, and ``pause``,
    # ``clear`` and ``led:off`` leave it as dark as it can go.

    async def screen_on(self) -> None:
        """Wake the screen at start-up, in case a shutdown left it dark."""
        await self._write_quietly(("screen:on",) if self._supports_push else ("resume",))

    async def screen_off(self) -> None:
        """Blank the screen before the machine goes down."""
        await self._write_quietly(
            ("screen:off",) if self._supports_push else ("pause", "clear", "led:off")
        )

    async def request_report(self) -> None:
        """Ask for a full report. Old firmware answers ERR, which is ignored."""
        await self._write_quietly(("report",))

    async def _write_quietly(self, lines: tuple[str, ...]) -> bool:
        for line in lines:
            try:
                await self._backend.write_line(line)
            except Exception:
                log.warning("CYD write failed for %r", line)
                return False
        return True

    # ---- Writer ----

    async def send_curated(self, verb: str, value: str | None) -> str | None:
        """Send a curated ``cyd.*`` verb to the CYD.

        ``mood``, ``mood_cycle`` and ``bright`` are checked first, and ``mood``
        is sent in upper case. Sending ``mood``, ``mood_cycle``, ``bright`` or
        ``face`` records the value in the screen state at once, so consoles
        agree even with firmware that never reports; a report overrides it.

        Args:
            verb: e.g. ``'cyd.mood'``
            value: optional payload string (e.g. ``'HAPPY'``); if None, the
                serial line has no colon-payload (``blink\\n``).

        Returns:
            The value as sent, e.g. ``'HAPPY'`` for ``'happy'``.

        Raises:
            KeyError: if ``verb`` isn't in the curated map.
            ValueError: if a checked verb's value is not accepted. Nothing
                is written.
        """
        serial_name = _CYD_VERB_TO_SERIAL[verb]  # raises KeyError on miss
        check = _VALUE_CHECKS.get(verb)
        if check is not None:
            value = check(value)
        line = serial_name if value is None else f"{serial_name}:{value}"
        await self._backend.write_line(line)
        await self._record_sent(verb, value)
        return value

    async def _record_sent(self, verb: str, value: str | None) -> None:
        if verb == "cyd.mood":
            await self._update_screen(mood=value)
        elif verb == "cyd.mood_cycle":
            await self._update_screen(cycle=value == "on")
        elif verb == "cyd.bright":
            # The firmware's bright: also turns auto-brightness off.
            await self._update_screen(bright=int(value), auto_bright=False)
        elif verb == "cyd.face" and value is not None:
            await self._update_screen(mood=CUSTOM_FACE)

    async def send_raw(self, line: str) -> None:
        """Escape hatch: write ``line`` verbatim to the CYD. Newline is added
        by the SerialBackend."""
        await self._backend.write_line(line)