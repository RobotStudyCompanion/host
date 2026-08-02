"""Robot connection: WebSocket lifecycle, verb dispatch, event streaming.

The single entry point is :func:`connect` — an ``async with`` context manager
that resolves the target (by robot name, hostname, or explicit URL),
authenticates, and yields a :class:`Robot` ready for use.

The :class:`Robot` class exposes:

* :meth:`Robot.call` — send a verb, await the ack. Raises :class:`RSCError`
  on failure. This is the workhorse — every verb (typed or not) goes through it.

* :meth:`Robot.events` — async iterator yielding :class:`Event` records.
  Optional ``topic`` and ``source`` filters. Optional ``replay_since_seq``
  to catch up on missed events via the ``events.history`` verb.

* Convenience wrappers (:meth:`Robot.ping`, :meth:`Robot.status`, and the
  peripheral proxies attached later) — thin shims over :meth:`Robot.call`.

Under the hood: one WebSocket connection per :class:`Robot`. A background rx
task reads frames and demuxes into two flows — acks (correlated by ``id``)
and events (fanned out to subscriber queues).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from rsc_host.client.discovery import RobotInfo, resolve
from rsc_host.client.errors import (
    ConnectionRefused,
    DisconnectedError,
    RSCError,
)
from rsc_host.protocol import Ack, Event

log = logging.getLogger(__name__)


class Robot:
    """Authenticated connection to a running rsc-host daemon.

    Not constructed directly — use :func:`connect`.
    """

    def __init__(self, ws, robot_info: RobotInfo | None = None) -> None:
        self._ws = ws
        self.info = robot_info

        # Pending acks: {id: Future[Ack]}
        self._pending: dict[str, asyncio.Future[Ack]] = {}
        # Event subscriber queues, drained by events() iterators.
        self._event_subscribers: set[asyncio.Queue[Event]] = set()
        self._rx_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

    async def _start_rx(self) -> None:
        """Kick off the background receive loop. Called by :func:`connect`."""
        self._rx_task = asyncio.create_task(self._rx_loop(), name="rsc-client-rx")

    async def _rx_loop(self) -> None:
        """Read WS messages, demux acks / events / errors."""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    log.debug("client: dropped binary frame (%d bytes)", len(raw))
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("client: dropped invalid JSON: %r", raw[:80])
                    continue

                msg_type = payload.get("type")
                if msg_type == "ack":
                    self._handle_ack(payload)
                elif msg_type == "event":
                    self._handle_event(payload)
                else:
                    log.debug("client: dropped unknown message type: %r", msg_type)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("client rx loop crashed")
        finally:
            self._closed.set()
            # Fail any outstanding acks.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DisconnectedError("connection closed"))
            self._pending.clear()
            # Signal event subscribers to end.
            for q in self._event_subscribers:
                try:
                    q.put_nowait(None)  # type: ignore[arg-type]
                except asyncio.QueueFull:
                    pass

    def _handle_ack(self, payload: dict) -> None:
        try:
            ack = Ack.model_validate(payload)
        except Exception:
            log.exception("client: malformed ack: %s", payload)
            return
        fut = self._pending.pop(ack.id, None)
        if fut is None or fut.done():
            log.debug("client: ack for unknown id=%s", ack.id)
            return
        fut.set_result(ack)

    def _handle_event(self, payload: dict) -> None:
        try:
            event = Event.model_validate(payload)
        except Exception:
            log.exception("client: malformed event: %s", payload)
            return
        for q in self._event_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "client: dropping event for slow local subscriber (topic=%s)",
                    event.topic,
                )

    # ---- Public API ----

    async def call(
        self,
        verb: str,
        timeout: float = 10.0,
        **args: Any,
    ) -> dict[str, Any] | None:
        """Send ``verb`` to the daemon, await the ack, return ``ack.result``.

        Args:
            verb:    Dotted verb name (e.g. ``'flipper.left'``, ``'cyd.mood'``).
            timeout: Seconds to wait for the ack.
            **args:  Verb arguments (become ``args`` in the wire message).

        Returns:
            The ack's ``result`` dict (may be None).

        Raises:
            RSCError:          Daemon returned ``ok=false``.
            DisconnectedError: Connection lost before the ack arrived.
            TimeoutError:      Ack didn't arrive within ``timeout``.
        """
        if self._closed.is_set():
            raise DisconnectedError("robot connection is closed")

        cmd_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future[Ack] = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = fut

        payload = {
            "type": "cmd",
            "id":   cmd_id,
            "verb": verb,
            "args": args,
        }
        try:
            await self._ws.send(json.dumps(payload))
        except ConnectionClosed as exc:
            self._pending.pop(cmd_id, None)
            raise DisconnectedError(f"send failed: {exc}") from exc

        try:
            ack = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise TimeoutError(
                f"no ack for verb={verb!r} within {timeout}s"
            ) from None

        if not ack.ok:
            raise RSCError(
                code=ack.code.value if ack.code else "UNKNOWN",
                message=ack.message or "",
                verb=verb,
            )
        return ack.result

    @asynccontextmanager
    async def _subscribe(self) -> AsyncIterator[asyncio.Queue]:
        """Register a local event queue for the duration of the context."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._event_subscribers.add(queue)
        try:
            yield queue
        finally:
            self._event_subscribers.discard(queue)

    async def events(
        self,
        *,
        topic: str | None = None,
        source: str | None = None,
        replay_since_seq: int | None = None,
        replay_history: bool = False,
    ) -> AsyncIterator[Event]:
        """Async-iterate events, optionally filtered.

        Args:
            topic:            If set, yield only events with this exact topic.
            source:           If set, yield only ``'host'`` or ``'cyd'``.
            replay_since_seq: If set, request replay of buffered events with
                              ``seq >= replay_since_seq`` before streaming new
                              ones. Combines with ``topic`` on the daemon side.
            replay_history:   Shortcut — replay everything the daemon still
                              has buffered before streaming new events.

        Usage::

            async for event in robot.events(topic="button.press"):
                print("pressed")
                break
        """
        # Optional history replay first.
        if replay_history or replay_since_seq is not None:
            args: dict[str, Any] = {}
            if replay_since_seq is not None:
                args["since_seq"] = replay_since_seq
            if topic is not None:
                args["topic"] = topic
            result = await self.call("events.history", **args)
            for e_dict in (result or {}).get("events", []):
                try:
                    e = Event.model_validate(e_dict)
                except Exception:
                    log.exception("client: dropped malformed replay event")
                    continue
                if source is not None and e.source != source:
                    continue
                yield e

        # Now stream live events.
        async with self._subscribe() as queue:
            while True:
                event = await queue.get()
                if event is None:  # sentinel: connection closed
                    return
                if topic is not None and event.topic != topic:
                    continue
                if source is not None and event.source != source:
                    continue
                yield event

    async def close(self) -> None:
        """Close the connection cleanly."""
        try:
            await self._ws.close()
        finally:
            if self._rx_task is not None and not self._rx_task.done():
                self._rx_task.cancel()
                try:
                    await self._rx_task
                except (asyncio.CancelledError, Exception):
                    pass

    # ---- Convenience wrappers over call() ----
    # Thin shims: same shape as call(), just typed for editor autocomplete.
    # Any unknown verb still works via .call() directly.

    async def ping(self) -> dict:
        return await self.call("ping") or {}

    async def status(self) -> dict:
        return await self.call("status") or {}

    async def flipper_left(self, speed: float, ramp_ms: int = 0) -> dict:
        return await self.call("flipper.left", speed=speed, ramp_ms=ramp_ms) or {}

    async def flipper_right(self, speed: float, ramp_ms: int = 0) -> dict:
        return await self.call("flipper.right", speed=speed, ramp_ms=ramp_ms) or {}

    async def flipper_left_stop(self) -> dict:
        return await self.call("flipper.left.stop") or {}

    async def flipper_right_stop(self) -> dict:
        return await self.call("flipper.right.stop") or {}

    async def ring_mode(
        self, mode: str, **params: Any
    ) -> dict:
        return await self.call("ring.mode", mode=mode, params=params) or {}

    async def button_led(self, mode: str, **params: Any) -> dict:
        return await self.call("button_led", mode=mode, params=params) or {}

    async def cyd(self, verb: str, value: str | None = None) -> dict:
        """Send a curated cyd.* verb (e.g. cyd('mood', 'HAPPY'))."""
        return await self.call(f"cyd.{verb}", value=value) or {}

    async def audio_devices(self) -> dict:
        return await self.call("audio.devices") or {}


# ---- connect() ----


async def _resolve_target(target: str, token_hint: str | None = None) -> tuple[str, RobotInfo | None]:
    """Turn a user-supplied target into a ws:// URL.

    Handles:
      * explicit ws:// or wss:// URL — used as-is
      * ``host:port`` or ``host`` — assumed ws:// on 8765
      * plain robot name — mDNS lookup, fallback to ``<name>.local:8765``
    """
    if target.startswith(("ws://", "wss://")):
        return target, None

    if "." in target or ":" in target:
        # Hostname or host:port. Trust the user.
        if ":" not in target:
            target = f"{target}:8765"
        return f"ws://{target}", None

    # Robot name. Try mDNS first.
    info = await resolve(target, timeout=2.0)
    if info is not None:
        return info.url, info
    # Fallback: assume <name>.local.
    return f"ws://{target}.local:8765", None


@asynccontextmanager
async def connect(
    target: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> AsyncIterator[Robot]:
    """Connect to a running rsc-host daemon.

    Args:
        target: One of
            * robot name (``'shiny'``) — resolves via mDNS, falls back to
              ``ws://shiny.local:8765``
            * hostname (``'shiny.local'``) — used as ``ws://shiny.local:8765``
            * host:port (``'shiny.local:8765'``)
            * full URL (``'ws://192.168.1.5:8765'``)
        token:  Bearer token. Defaults to ``$RSC_TOKEN`` env var.
        timeout: Handshake timeout in seconds.

    Usage::

        async with connect("shiny", token="dev") as robot:
            await robot.ping()
            await robot.flipper_left(0.5, ramp_ms=500)

    Raises:
        ConnectionRefused: Handshake failed (bad token, wrong host, refused).
    """
    resolved_token = token if token is not None else os.environ.get("RSC_TOKEN", "").strip()
    if not resolved_token:
        raise ConnectionRefused(
            "no token supplied — pass token= or set RSC_TOKEN"
        )

    url, info = await _resolve_target(target, resolved_token)

    try:
        ws = await asyncio.wait_for(
            ws_connect(
                url,
                subprotocols=["bearer", resolved_token],  # type: ignore[list-item]
                open_timeout=timeout,
            ),
            timeout=timeout,
        )
    except InvalidStatus as exc:
        if exc.response.status_code == 401:
            raise ConnectionRefused("unauthorized (bad token)") from exc
        raise ConnectionRefused(f"handshake rejected: {exc.response.status_code}") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise ConnectionRefused(f"cannot reach {url}: {exc}") from exc

    robot = Robot(ws, robot_info=info)
    await robot._start_rx()
    try:
        yield robot
    finally:
        await robot.close()
