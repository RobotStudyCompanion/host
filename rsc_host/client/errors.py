"""Client-side exception types.

Design:

* :class:`RSCError` — raised when the daemon returns ``ok=false``. Carries
  the machine-readable ``code`` (matching :class:`~rsc_host.protocol.ErrorCode`)
  and human message. This is what callers usually catch.

* :class:`DisconnectedError` — raised when the WebSocket closes unexpectedly
  (transport-level, not a protocol-level failure). Distinct from
  :class:`RSCError` so callers can distinguish "the robot said no" from
  "the connection dropped".

* :class:`TimeoutError` — command dispatched, no reply within the deadline.
  Aliased to the stdlib name so idiomatic catches work.
"""
from __future__ import annotations


class RSCError(Exception):
    """The daemon returned a failure Ack.

    Attributes:
        code:    Stable error code string (e.g. ``INVALID_ARGS``,
                 ``UNKNOWN_VERB``, ``PERIPHERAL_BUSY``).
        message: Human-readable detail from the daemon.
        verb:    The verb that failed, if known.
    """

    def __init__(self, code: str, message: str, verb: str | None = None) -> None:
        self.code = code
        self.message = message
        self.verb = verb
        super().__init__(f"[{code}] {message}" + (f" (verb={verb})" if verb else ""))


class DisconnectedError(Exception):
    """The WebSocket closed unexpectedly.

    Raised from ``call()``/event iteration when the connection to the daemon
    is lost. Not raised on graceful ``async with`` exit.
    """


class ConnectionRefused(Exception):
    """Initial handshake failed — unreachable host, bad token, or wrong path.

    Attributes:
        reason: Best-effort human description.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
