"""Verb registry and dispatch for the rsc-host WebSocket API.

A handler is registered against a dotted verb name via the ``@verb`` decorator,
declaring a pydantic args model for input validation. The dispatcher:

  1. looks up the handler for an incoming :class:`Cmd`,
  2. validates ``cmd.args`` against the handler's args model,
  3. ``await``s the handler,
  4. wraps the result (or any raised exception) into an :class:`Ack`.

Handlers must be async; long-running work should not block the event loop.

Usage::

    from pydantic import BaseModel
    from rsc_host.dispatch import verb

    class FlipperArgs(BaseModel):
        speed: float
        ramp_ms: int = 0

    @verb("flipper.left", args_model=FlipperArgs)
    async def flipper_left(args: FlipperArgs) -> dict:
        # ... drive the servo ...
        return {"speed": args.speed}
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from rsc_host.protocol import Ack, Cmd, ErrorCode

log = logging.getLogger(__name__)

# A handler takes a validated args model instance and returns a result dict (or None).
Handler = Callable[[Any], Awaitable[dict[str, Any] | None]]


class _Registration:
    """Internal binding of (verb name, args schema, handler coroutine)."""

    __slots__ = ("verb", "args_model", "handler")

    def __init__(
        self,
        verb: str,
        args_model: type[BaseModel],
        handler: Handler,
    ) -> None:
        self.verb = verb
        self.args_model = args_model
        self.handler = handler


class Dispatcher:
    """Registry of verb → handler bindings, plus the dispatch coroutine.

    A single :class:`Dispatcher` instance is typically shared across the server;
    peripherals register against the module-level :data:`default_dispatcher`.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}

    def verb(
        self,
        name: str,
        args_model: type[BaseModel],
    ) -> Callable[[Handler], Handler]:
        """Decorator: register ``func`` under verb ``name`` with the given args schema.

        Raises:
            ValueError: if ``name`` is already registered.
            TypeError:  if ``func`` is not an ``async def`` coroutine.
        """

        def decorator(func: Handler) -> Handler:
            if name in self._registrations:
                raise ValueError(f"verb already registered: {name!r}")
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"handler for {name!r} must be an async function "
                    f"(got {type(func).__name__})"
                )
            self._registrations[name] = _Registration(name, args_model, func)
            log.debug("registered verb: %s -> %s", name, func.__qualname__)
            return func

        return decorator

    def registered_verbs(self) -> list[str]:
        """Sorted list of registered verb names. Useful for `help` / introspection."""
        return sorted(self._registrations.keys())

    async def dispatch(self, cmd: Cmd) -> Ack:
        """Validate, route, execute the handler for ``cmd``, and pack the response.

        Always returns an :class:`Ack`. Never raises. Handler exceptions are
        logged server-side and surface as :attr:`ErrorCode.INTERNAL_ERROR`
        with a generic message — internal traces never reach the wire.
        """
        reg = self._registrations.get(cmd.verb)
        if reg is None:
            return Ack.failure(
                cmd.id, ErrorCode.UNKNOWN_VERB, f"no handler for verb {cmd.verb!r}"
            )

        try:
            validated = reg.args_model.model_validate(cmd.args)
        except ValidationError as exc:
            return Ack.failure(cmd.id, ErrorCode.INVALID_ARGS, str(exc))

        try:
            result = await reg.handler(validated)
        except Exception:
            log.exception(
                "handler raised for verb=%s id=%s", cmd.verb, cmd.id
            )
            return Ack.failure(
                cmd.id,
                ErrorCode.INTERNAL_ERROR,
                "handler raised; see host logs",
            )

        # Handlers may return None for fire-and-forget verbs; coerce to None
        # rather than serialising whatever non-dict value snuck through.
        return Ack.success(
            cmd.id,
            result=result if isinstance(result, dict) else None,
        )


#: Module-level default. Peripherals register against this via ``rsc_host.dispatch.verb``.
default_dispatcher = Dispatcher()

#: Shorthand: ``from rsc_host.dispatch import verb`` then ``@verb("...", args_model=...)``.
verb = default_dispatcher.verb
