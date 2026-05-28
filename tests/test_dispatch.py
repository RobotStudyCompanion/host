"""Dispatch behaviour: registration rules, lookup, validation, execution, errors.

Covers:
  - Async handlers register; sync handlers are rejected
  - Duplicate registration is rejected (catches accidental name collisions early)
  - Unknown verbs return UNKNOWN_VERB without crashing
  - Args validation errors surface as INVALID_ARGS with the validator's detail
  - Handler exceptions surface as INTERNAL_ERROR, *without leaking the trace*
  - Handlers returning ``None`` produce an ok=True Ack with result=None
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from rsc_host.dispatch import Dispatcher
from rsc_host.protocol import Cmd, ErrorCode


class _FlipperArgs(BaseModel):
    speed: float
    ramp_ms: int = 0


class _EmptyArgs(BaseModel):
    pass


@pytest.fixture
def dispatcher() -> Dispatcher:
    """Fresh dispatcher per test — registrations don't leak across cases."""
    return Dispatcher()


class TestRegistration:
    def test_register_async_handler(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("flipper.left", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            return {"speed": args.speed}

        assert "flipper.left" in dispatcher.registered_verbs()

    def test_registered_verbs_sorted(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("z.last", args_model=_EmptyArgs)
        async def _z(args: _EmptyArgs) -> dict:
            return {}

        @dispatcher.verb("a.first", args_model=_EmptyArgs)
        async def _a(args: _EmptyArgs) -> dict:
            return {}

        assert dispatcher.registered_verbs() == ["a.first", "z.last"]

    def test_duplicate_registration_raises(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("status", args_model=_EmptyArgs)
        async def _h1(args: _EmptyArgs) -> dict:
            return {}

        with pytest.raises(ValueError, match="already registered"):

            @dispatcher.verb("status", args_model=_EmptyArgs)
            async def _h2(args: _EmptyArgs) -> dict:
                return {}

    def test_sync_handler_rejected(self, dispatcher: Dispatcher) -> None:
        with pytest.raises(TypeError, match="must be an async function"):

            @dispatcher.verb("sync.handler", args_model=_EmptyArgs)
            def _sync(args: _EmptyArgs) -> dict:  # type: ignore[misc]
                return {}


class TestDispatch:
    async def test_happy_path(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("flipper.left", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            return {"echoed_speed": args.speed, "ramp_ms": args.ramp_ms}

        cmd = Cmd(id="c1", verb="flipper.left", args={"speed": 0.5, "ramp_ms": 200})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is True
        assert ack.id == "c1"
        assert ack.result == {"echoed_speed": 0.5, "ramp_ms": 200}
        assert ack.code is None
        assert ack.message is None

    async def test_default_args_applied(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("flipper.left", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            return {"ramp_ms": args.ramp_ms}

        cmd = Cmd(id="c1", verb="flipper.left", args={"speed": 0.5})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is True
        assert ack.result == {"ramp_ms": 0}  # default applied

    async def test_unknown_verb(self, dispatcher: Dispatcher) -> None:
        cmd = Cmd(id="c2", verb="nope.never", args={})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is False
        assert ack.code == ErrorCode.UNKNOWN_VERB
        assert ack.id == "c2"

    async def test_invalid_args_wrong_type(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("flipper.left", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            return {}

        cmd = Cmd(id="c3", verb="flipper.left", args={"speed": "not-a-number"})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is False
        assert ack.code == ErrorCode.INVALID_ARGS

    async def test_invalid_args_missing_required(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("flipper.left", args_model=_FlipperArgs)
        async def _handler(args: _FlipperArgs) -> dict:
            return {}

        cmd = Cmd(id="c3b", verb="flipper.left", args={})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is False
        assert ack.code == ErrorCode.INVALID_ARGS

    async def test_handler_exception_wrapped(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("boom", args_model=_EmptyArgs)
        async def _handler(args: _EmptyArgs) -> dict:
            raise RuntimeError("hardware fell over")

        cmd = Cmd(id="c4", verb="boom", args={})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is False
        assert ack.code == ErrorCode.INTERNAL_ERROR
        # Crucially: internal trace detail must not leak to the wire.
        assert "hardware fell over" not in (ack.message or "")

    async def test_handler_returning_none(self, dispatcher: Dispatcher) -> None:
        @dispatcher.verb("fire-and-forget", args_model=_EmptyArgs)
        async def _handler(args: _EmptyArgs) -> None:
            return None

        cmd = Cmd(id="c5", verb="fire-and-forget", args={})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is True
        assert ack.result is None

    async def test_handler_returning_non_dict_coerced_to_none(
        self, dispatcher: Dispatcher
    ) -> None:
        # Handlers should return dict | None; if a handler accidentally returns
        # something else (e.g. a string), the dispatcher coerces to None rather
        # than serialising garbage. Defensive belt-and-braces.
        @dispatcher.verb("misbehaving", args_model=_EmptyArgs)
        async def _handler(args: _EmptyArgs) -> dict:
            return "I am not a dict"  # type: ignore[return-value]

        cmd = Cmd(id="c6", verb="misbehaving", args={})
        ack = await dispatcher.dispatch(cmd)

        assert ack.ok is True
        assert ack.result is None
