"""Wire protocol for the rsc-host WebSocket API.

Three message shapes traverse the wire (JSON, one message per frame):

  Client → host
    Cmd     — { "type": "cmd",   "id": "...", "verb": "...", "args": {...} }

  Host → client (reply to a Cmd)
    Ack     — { "type": "ack",   "id": "...", "ok": true,  "result": {...} }
            — { "type": "ack",   "id": "...", "ok": false, "code": "...", "message": "..." }

  Host → client (broadcast)
    Event   — { "type": "event", "topic": "...", "source": "host"|"cyd", "data": {...} }

The CYD UART grammar is mirrored as events tagged source="cyd"; commands toward
the CYD are dispatched on dotted verbs (e.g. cyd.mood) and translated to the
underscored serial form by the CYD bridge.

All models forbid extra fields. Protocol drift between client and host should
surface as a validation error, not as silently-dropped data.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """Stable error codes returned in failure Acks.

    New codes append; existing codes never change semantics. Clients should
    treat unknown codes as equivalent to INTERNAL_ERROR for forward compat.
    """

    INVALID_MESSAGE = "INVALID_MESSAGE"
    """Top-level message failed schema validation (bad JSON, wrong type, etc.)."""

    UNKNOWN_VERB = "UNKNOWN_VERB"
    """Verb is not registered on this host."""

    INVALID_ARGS = "INVALID_ARGS"
    """Args failed validation against the verb's argument schema."""

    PERIPHERAL_BUSY = "PERIPHERAL_BUSY"
    """Peripheral is in use by another command and cannot accept overlap."""

    PERIPHERAL_UNAVAILABLE = "PERIPHERAL_UNAVAILABLE"
    """Peripheral is unreachable (hardware absent, driver not loaded, etc.)."""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    """Connection lacks a valid auth token."""

    RATE_LIMITED = "RATE_LIMITED"
    """Caller exceeded the per-client rate limit on this verb."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Unexpected exception in the handler. Detail is in the host logs, not the wire."""


class Cmd(BaseModel):
    """A command from a client to the host."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cmd"] = "cmd"
    id: str = Field(..., description="Client-chosen correlation ID; echoed in the Ack.")
    verb: str = Field(..., description="Dotted verb name, e.g. 'flipper.left' or 'cyd.mood'.")
    args: dict[str, Any] = Field(default_factory=dict)


class Ack(BaseModel):
    """Reply to a Cmd — either success with a result, or failure with a code + message."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ack"] = "ack"
    id: str = Field(..., description="Correlation ID from the originating Cmd.")
    ok: bool
    result: dict[str, Any] | None = Field(
        default=None,
        description="Set when ok=true; the handler's return value, or None if it returned nothing.",
    )
    code: ErrorCode | None = Field(
        default=None,
        description="Set when ok=false; stable enum, never changes meaning.",
    )
    message: str | None = Field(
        default=None,
        description="Set when ok=false; human-readable detail. Never leaks internal traces.",
    )

    @classmethod
    def success(cls, id_: str, result: dict[str, Any] | None = None) -> "Ack":
        """Build a success Ack. `result` is whatever the handler returned (or None)."""
        return cls(id=id_, ok=True, result=result)

    @classmethod
    def failure(cls, id_: str, code: ErrorCode, message: str) -> "Ack":
        """Build a failure Ack. Caller is responsible for sanitising `message`."""
        return cls(id=id_, ok=False, code=code, message=message)


class Event(BaseModel):
    """An asynchronous event broadcast from the host to subscribed clients.

    Events are fire-and-forget — no correlation ID, no ack. Sources are bounded
    to 'host' (events the host itself emits — peripheral state, button edges,
    versioning) and 'cyd' (events ingested from the front-panel UART).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    topic: str = Field(..., description="Dotted topic, e.g. 'host_vol' or 'flipper.left.state'.")
    source: Literal["host", "cyd"] = Field(
        ...,
        description="'cyd' for events ingested from the front-panel UART; 'host' otherwise.",
    )
    data: dict[str, Any] = Field(default_factory=dict)
