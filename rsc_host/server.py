"""WebSocket server tying dispatch, the event bus, and auth to the wire.

Three WebSocket endpoints on one port:

  * ``/``           — JSON control channel (cmd/ack/event). One per client.
  * ``/audio/out``  — client → server. Binary WAV chunks concatenated into
                       one clip; playback begins after the client closes the
                       stream. Requires the same bearer auth.
  * ``/audio/in``   — server → client. Binary PCM frames from the current
                       capture session. Closed by the server when capture
                       ends, or by the client to end the session early.

Extra handlers register via :meth:`Server.add_path_handler`. The audio
peripheral wires its two handlers this way — keeps ``server.py`` transport-
only, no audio-specific code here.

Each connection still authenticates on handshake (bearer token via
``Sec-WebSocket-Protocol``). Path routing happens after auth.

When a control connection ends, however it ended, whatever it left moving is
stopped: see :mod:`rsc_host.motion`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import websockets
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from rsc_host.auth import AuthError, TokenAuth
from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.motion import MotionOwners, Owner, current_owner
from rsc_host.protocol import Ack, Cmd, ErrorCode, Event

log = logging.getLogger(__name__)

PathHandler = Callable[[ServerConnection], Awaitable[None]]

# A peer that vanishes without closing (Wi-Fi lost, laptop asleep) is noticed
# after at most PING_INTERVAL_S + PING_TIMEOUT_S + CLOSE_TIMEOUT_S, measured at
# 12 s against a silent peer, and that is how long its motion can outlive it.
# The close timeout counts because the library waits that long for a dead peer
# to answer the close before it drops the socket; a healthy close takes
# milliseconds. A false positive costs a reconnect and a stopped flipper, which
# is the safe way to be wrong.
PING_INTERVAL_S = 5.0
PING_TIMEOUT_S = 5.0
CLOSE_TIMEOUT_S = 2.0

# Frames buffered per connection while a command is running, one at a time per
# socket. When this fills the library stops reading, pongs go unread behind the
# frames, and the keepalive drops the client. Measured, the size a client needs is
# rate * (command seconds - PING_TIMEOUT_S). The console's own longest command is
# its self-test, about 6.5 s, and its sliders send at most 10 Hz per control, so
# two dragged at once need 30. Worst-case memory is this times max_size (1 MiB).
# A longer command with sustained input, such as a 30 s self-test from the Advanced
# panel with a slider dragged throughout, gets dropped, which stops its motion.
MAX_QUEUED_FRAMES = 32

# How long a closed connection's receive loop gets to finish by itself. Frames
# that arrived before the close still run, as fire-and-forget clients expect.
_RX_DRAIN_S = 0.2


class Server:
    """WebSocket server bound to a :class:`Dispatcher` and an :class:`EventBus`.

    Not started on construction; call :meth:`serve_forever` (or the async
    context manager form via :meth:`start` / :meth:`stop`).
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        bus: EventBus,
        auth: TokenAuth,
        *,
        bind: str = "127.0.0.1",
        port: int = 8765,
        ssl_context: ssl.SSLContext | None = None,
        motion: MotionOwners | None = None,
        ping_interval: float = PING_INTERVAL_S,
        ping_timeout: float = PING_TIMEOUT_S,
        close_timeout: float = CLOSE_TIMEOUT_S,
    ) -> None:
        self._dispatcher = dispatcher
        self._bus = bus
        self._auth = auth
        self._bind = bind
        self._port = port
        self._ssl = ssl_context
        self._motion = motion if motion is not None else MotionOwners()
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._close_timeout = close_timeout
        self._server: websockets.asyncio.server.Server | None = None
        self._path_handlers: dict[str, PathHandler] = {}
        # Connection cleanups still running. Held here so a cancelled handler
        # can't take its cleanup with it, and so stop() can wait for them.
        self._endings: set[asyncio.Task[None]] = set()

    # ---- Extension ----

    def add_path_handler(self, path: str, handler: PathHandler) -> None:
        """Register ``handler`` for connections whose request path is ``path``.

        Handlers are called after auth, with an authenticated
        :class:`ServerConnection`. They own the connection until they return.

        Raises:
            ValueError: if ``path`` is already registered or is ``'/'``
                        (reserved for the JSON control channel).
        """
        if path == "/":
            raise ValueError("path '/' is reserved for the JSON control channel")
        if path in self._path_handlers:
            raise ValueError(f"path handler already registered: {path!r}")
        self._path_handlers[path] = handler
        log.debug("registered path handler: %s", path)

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Bind and begin accepting connections. Does not block."""
        self._server = await serve(
            self._handle_connection,
            host=self._bind,
            port=self._port,
            ssl=self._ssl,
            # Reflect the client's bearer subprotocol on success. The
            # process_request hook decides whether to accept the handshake.
            subprotocols=["bearer"],  # advertised for the negotiation step
            select_subprotocol=self._select_subprotocol,
            process_request=self._process_request,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=self._close_timeout,
            max_queue=MAX_QUEUED_FRAMES,
        )
        scheme = "wss" if self._ssl else "ws"
        log.info("host serving on %s://%s:%d", scheme, self._bind, self._port)

    async def stop(self) -> None:
        """Stop accepting connections and wait for in-flight ones to drain."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            await asyncio.gather(*self._endings, return_exceptions=True)
            log.info("host stopped")

    async def serve_forever(self) -> None:
        """Start and block until cancelled. Convenience for ``__main__``."""
        await self.start()
        try:
            await asyncio.Future()  # sleep forever; cancelled on shutdown
        finally:
            await self.stop()

    # ---- Handshake hooks ----

    def _select_subprotocol(
        self,
        connection: ServerConnection,
        subprotocols: list[str],
    ) -> str | None:
        """Reflect ``bearer`` so the client sees a successful negotiation.

        We already validated the token in :meth:`_process_request`; here we
        only pick which subprotocol string to echo. Return the literal 'bearer'
        (not the token itself — that shouldn't appear in server response
        headers where it might get logged by intermediaries).
        """
        return "bearer" if "bearer" in subprotocols else None

    def _process_request(
        self,
        connection: ServerConnection,
        request: websockets.http11.Request,
    ) -> websockets.http11.Response | None:
        """Reject bad-token handshakes before the WebSocket upgrade completes.

        Returning ``None`` means "carry on with the upgrade"; returning a
        Response short-circuits with an HTTP error.
        """
        raw = request.headers.get("Sec-WebSocket-Protocol", "")
        subprotocols = [s.strip() for s in raw.split(",")] if raw else []
        token = self._auth.extract_from_subprotocols(subprotocols)
        try:
            self._auth.check(token)
        except AuthError as exc:
            log.warning(
                "handshake rejected from %s: %s",
                connection.remote_address,
                exc,
            )
            # 401 with no body — clients get a clear signal without leaking
            # whether the token was missing vs merely wrong.
            return connection.respond(401, "unauthorized\n")
        return None

    # ---- Per-connection loop ----

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Route to path-specific handler, or fall through to JSON control."""
        peer = ws.remote_address
        raw_path = ws.request.path if ws.request else "/"
        # Route on the path alone. A client appending a query string (a
        # cache-buster, a client id) would otherwise miss its handler entirely
        # and silently fall through to the JSON control loop.
        path = urlsplit(raw_path).path or "/"
        log.info("client connected: %s path=%s", peer, path)

        try:
            # Path routing: extension handlers take precedence for their paths;
            # everything else runs the standard JSON control loop.
            handler = self._path_handlers.get(path)
            if handler is not None:
                try:
                    await handler(ws)
                except ConnectionClosed:
                    pass
                except Exception:
                    log.exception("path handler failed: path=%s peer=%s", path, peer)
                return

            await self._json_control_loop(ws)
        finally:
            log.info("client disconnected: %s path=%s", peer, path)

    async def _json_control_loop(self, ws: ServerConnection) -> None:
        """Standard JSON control channel: rx dispatches cmds, tx broadcasts events."""
        peer = ws.remote_address
        owner = Owner(str(peer))
        async with self._bus.subscribe() as queue:
            rx_task = asyncio.create_task(self._rx_loop(ws, owner), name=f"rx-{peer}")
            tx_task = asyncio.create_task(
                self._tx_loop(ws, queue), name=f"tx-{peer}"
            )
            # Watched separately because neither loop notices a close in time:
            # rx may be busy in a long command, and tx only finds out on its next
            # send. Motion must not wait for either.
            closed_task = asyncio.create_task(ws.wait_closed(), name=f"closed-{peer}")
            try:
                await asyncio.wait(
                    {rx_task, tx_task, closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                log.exception("json control loop failed")
            finally:
                # In its own task and shielded, so that cancelling this handler
                # can't abandon the cleanup halfway.
                ending = asyncio.create_task(
                    self._end_connection(owner, rx_task, tx_task, closed_task),
                    name=f"end-{peer}",
                )
                self._endings.add(ending)
                ending.add_done_callback(self._endings.discard)
                await asyncio.shield(ending)

    async def _end_connection(
        self,
        owner: Owner,
        rx_task: asyncio.Task[None],
        tx_task: asyncio.Task[None],
        closed_task: asyncio.Task[None],
    ) -> None:
        """Stop what ``owner`` left moving, then wind down its tasks."""
        await asyncio.wait({rx_task}, timeout=_RX_DRAIN_S)
        owner.closed = True
        # Now, rather than after the cancel below: a command that is slow to
        # cancel must not keep a flipper turning.
        await self._motion.stop_owned(owner, self._dispatcher)

        tasks = (rx_task, tx_task, closed_task)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Again, once rx has really finished. The command it was running may have
        # recorded a part after the pass above, or swallowed its cancellation
        # and gone on to move something. Nothing can start motion from here on.
        await self._motion.stop_owned(owner, self._dispatcher)

        for result in results:
            if isinstance(result, Exception) and not isinstance(result, ConnectionClosed):
                log.error("connection task failed", exc_info=result)

    async def _rx_loop(self, ws: ServerConnection, owner: Owner) -> None:
        """Read messages, dispatch, send Acks. Never exits on client-side errors."""
        current_owner.set(owner)
        async for raw in ws:
            if owner.closed:
                return
            if isinstance(raw, bytes):
                # We speak text JSON. Binary frames aren't part of the contract.
                await self._send_error(
                    ws,
                    id_="",
                    code=ErrorCode.INVALID_MESSAGE,
                    message="binary frames not supported; send JSON text",
                )
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                await self._send_error(
                    ws,
                    id_="",
                    code=ErrorCode.INVALID_MESSAGE,
                    message=f"invalid JSON: {exc.msg}",
                )
                continue

            try:
                cmd = Cmd.model_validate(payload)
            except ValidationError as exc:
                # Preserve the id if the client at least gave us one; otherwise
                # empty string so the client can still correlate against a
                # "last cmd" they know they sent.
                bad_id = ""
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    bad_id = payload["id"]
                await self._send_error(
                    ws,
                    id_=bad_id,
                    code=ErrorCode.INVALID_MESSAGE,
                    message=str(exc),
                )
                continue

            ack = await self._dispatcher.dispatch(cmd)
            await ws.send(ack.model_dump_json())

    async def _tx_loop(
        self,
        ws: ServerConnection,
        queue: asyncio.Queue[Event],
    ) -> None:
        """Drain the subscriber queue and forward events to the client."""
        while True:
            event = await queue.get()
            try:
                await ws.send(event.model_dump_json())
            except ConnectionClosed:
                return

    async def _send_error(
        self,
        ws: ServerConnection,
        *,
        id_: str,
        code: ErrorCode,
        message: str,
    ) -> None:
        """Convenience: build and send a failure Ack, swallowing send errors."""
        ack = Ack.failure(id_, code, message)
        try:
            await ws.send(ack.model_dump_json())
        except ConnectionClosed:
            pass


def build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """TLS context for the server. Straight PEM cert + key, no client-cert auth
    (deferred to a later layer)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


# ---- Convenience factory ----

BackendFactory = Callable[[], Awaitable[None]]
"""Placeholder — real backend setup lands with peripherals in the next layer."""
