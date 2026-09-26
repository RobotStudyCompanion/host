"""CYD (front-panel display) bridge.

Two-way UART link between the host daemon and the ESP32-driven CYD:

* **Reader task** — consumes newline-delimited lines from the SerialBackend.
  Lines starting with ``host_`` are classified into events tagged
  ``source: cyd`` and published on the bus. Everything else is logged and
  ignored (defensive against firmware chatter we don't yet handle).

* **Writer** — accepts dotted verbs from the daemon side (``cyd.mood``,
  ``cyd.theme``, …), translates to the underscored serial grammar
  (``mood:HAPPY``, ``theme:dark``, …), and writes to the SerialBackend.

The curated verb map is deliberately explicit — adding a verb is one line.
The ``cyd.raw`` escape hatch lets clients send anything to the CYD without
waiting for a daemon release, at the cost of coupling to the CYD's exact
line format.
"""
from __future__ import annotations

import asyncio
import logging

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

def _parse_int(payload: str) -> dict:
    return {"value": int(payload.strip())}


def _parse_empty(payload: str) -> dict:
    # host_mute, host_mic — no payload expected; ignore any trailing text.
    return {}


_CYD_EVENT_MAP: dict[str, tuple[str, callable]] = {
    "host_vol":       ("host_vol",      _parse_int),
    "host_mute":      ("host_mute",     _parse_empty),
    "host_mic":       ("host_mic",      _parse_empty),
    "host_reboot":    ("host_reboot",   _parse_empty),
    "host_poweroff":  ("host_poweroff", _parse_empty),
}


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


class CydBridge:
    """Bidirectional CYD link atop a :class:`SerialBackend`."""

    def __init__(self, backend: SerialBackend, bus: EventBus) -> None:
        self._backend = backend
        self._bus = bus
        self._reader_task: asyncio.Task[None] | None = None
        # None = not yet probed, True/False = firmware answered.
        self._supports_push: bool | None = None
        self._probe_reply: asyncio.Future[str] | None = None

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

        await self._bus.publish(Event(topic=topic, source="cyd", data=data))


    # ---- State push ----
    #
    # The panel keeps its own mute flags and flips them on every press, quite
    # independently of what the host decides. Mute from the console and the
    # panel's flag does not move; press the panel icon and it flips to whatever
    # it was not, which may now agree with the host or invert it. Two sources
    # of truth, drifting.
    #
    # The host is the authority — it owns the mixer — so it pushes state after
    # every change and the panel follows. That needs `vol`, `mute` and `mic`
    # commands in the CYD dispatch table, which do not exist yet. Rather than
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
            sent.append(f"mic:{'on' if mic_muted else 'off'}")
        for line in sent:
            try:
                await self._backend.write_line(line)
            except Exception:
                log.warning("CYD push failed for %r", line)
                return {"pushed": False, "reason": "serial write failed"}
        return {"pushed": True, "sent": sent}

    # ---- Writer ----

    async def send_curated(self, verb: str, value: str | None) -> None:
        """Send a curated ``cyd.*`` verb to the CYD.

        Args:
            verb: e.g. ``'cyd.mood'``
            value: optional payload string (e.g. ``'HAPPY'``); if None, the
                serial line has no colon-payload (``blink\\n``).

        Raises:
            KeyError: if ``verb`` isn't in the curated map.
        """
        serial_name = _CYD_VERB_TO_SERIAL[verb]  # raises KeyError on miss
        line = serial_name if value is None else f"{serial_name}:{value}"
        await self._backend.write_line(line)

    async def send_raw(self, line: str) -> None:
        """Escape hatch: write ``line`` verbatim to the CYD. Newline is added
        by the SerialBackend."""
        await self._backend.write_line(line)