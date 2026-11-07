"""Ownership rules in :mod:`rsc_host.motion`, without a server or ``setup()``.

The end-to-end versions, over real sockets and the fake backend, live in
``test_server.py``; these pin the rules themselves.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from pydantic import BaseModel

from rsc_host.dispatch import Dispatcher
from rsc_host.motion import Kind, MotionOwners, Owner, Part, current_owner

FLIPPER_LEFT = Part(Kind.FLIPPER, "left")
HOLD_LEFT = Part(Kind.HOLD, "left")
HOLD_RIGHT = Part(Kind.HOLD, "right")
FLIPPER_M3 = Part(Kind.FLIPPER, "m3")
HOLD_M3 = Part(Kind.HOLD, "m3")


class _Flipper:
    """All the policy reads from a flipper."""

    def __init__(self, servo_id: str, *, enabled: bool = True) -> None:
        self.id = servo_id
        self.enabled = enabled


LEFT = _Flipper("left")
M3 = _Flipper("m3", enabled=False)


@contextmanager
def calling_as(owner: Owner | None) -> Iterator[None]:
    token = current_owner.set(owner)
    try:
        yield
    finally:
        current_owner.reset(token)


@pytest.fixture
def motion() -> MotionOwners:
    return MotionOwners()


@pytest.fixture
def a() -> Owner:
    return Owner("a")


@pytest.fixture
def b() -> Owner:
    return Owner("b")


class TestRules:
    @pytest.mark.parametrize(
        "stop",
        [lambda m: m.speed_set(LEFT, 0.0), lambda m: m.flipper_stopped(LEFT)],
        ids=["speed zero", "stop"],
    )
    def test_speed_claims_and_a_stop_from_anyone_clears(self, motion, a, b, stop) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        assert motion.owned() == {FLIPPER_LEFT: a}
        with calling_as(b), stop(motion):
            pass
        assert motion.owned() == {}

    def test_takeover_moves_responsibility(self, motion, a, b) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        with calling_as(b), motion.speed_set(LEFT, 0.6):
            pass
        assert motion.owned() == {FLIPPER_LEFT: b}

    def test_hold_takeover_moves_responsibility(self, motion, a, b) -> None:
        with calling_as(a), motion.hold_started("left"):
            pass
        with calling_as(b), motion.hold_started("left"):
            pass
        assert motion.owned() == {HOLD_LEFT: b}

    def test_stopping_something_owns_nothing(self, motion, a) -> None:
        with calling_as(a), motion.flipper_stopped(LEFT):
            pass
        with calling_as(a), motion.hold_stopped("left"):
            pass
        assert motion.owned() == {}

    def test_speed_command_takes_a_held_servo_from_its_holder(self, motion, a, b) -> None:
        # Any speed that reaches the servo ends its hold.
        with calling_as(a), motion.hold_started("left"):
            pass
        with calling_as(b), motion.speed_set(LEFT, 0.5):
            pass
        assert motion.owned() == {FLIPPER_LEFT: b}

    def test_disabled_flipper_leaves_the_hold_alone(self, motion, a, b) -> None:
        # m3 never reaches the servo, so its hold carries on.
        with calling_as(a), motion.hold_started("m3"):
            pass
        with calling_as(b), motion.speed_set(M3, 0.5):
            pass
        assert motion.owned() == {HOLD_M3: a, FLIPPER_M3: b}
        with calling_as(b), motion.flipper_stopped(M3):
            pass
        assert motion.owned() == {HOLD_M3: a}

    def test_hold_takes_a_turning_flipper_from_its_owner(self, motion, a, b) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        with calling_as(b), motion.hold_started("left"):
            pass
        assert motion.owned() == {FLIPPER_LEFT: b, HOLD_LEFT: b}

    def test_hold_on_an_unowned_flipper_claims_only_the_hold(self, motion, b) -> None:
        with calling_as(b), motion.hold_started("left"):
            pass
        assert motion.owned() == {HOLD_LEFT: b}

    def test_hold_stop_with_an_id_releases_only_that_hold(self, motion, a, b) -> None:
        with calling_as(a), motion.hold_started("left"):
            pass
        with calling_as(b), motion.hold_started("right"):
            pass
        with calling_as(b), motion.hold_stopped("left"):
            pass
        assert motion.owned() == {HOLD_RIGHT: b}

    @pytest.mark.parametrize("servo_id", [None, ""])
    def test_hold_stop_without_id_clears_every_hold(self, motion, a, b, servo_id) -> None:
        with calling_as(a), motion.hold_started("left"):
            pass
        with calling_as(b), motion.hold_started("right"):
            pass
        with calling_as(a), motion.speed_set(_Flipper("m3"), 0.5):
            pass
        with calling_as(b), motion.hold_stopped(servo_id):
            pass
        # The flipper isn't a hold, so it stays owned.
        assert motion.owned() == {FLIPPER_M3: a}

    def test_command_from_inside_the_daemon_leaves_the_part_to_nobody(self, motion, a) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        with calling_as(None), motion.speed_set(LEFT, 0.6):
            pass
        assert motion.owned() == {}


class TestFailures:
    def test_failed_start_restores_the_previous_owner(self, motion, a, b) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        with pytest.raises(ValueError), calling_as(b), motion.speed_set(LEFT, 0.6):
            raise ValueError("speed must be in [-1.0, 1.0]")
        assert motion.owned() == {FLIPPER_LEFT: a}

    def test_failed_start_restores_the_hold_it_released(self, motion, a, b) -> None:
        with calling_as(a), motion.hold_started("left"):
            pass
        with pytest.raises(ValueError), calling_as(b), motion.speed_set(LEFT, 0.6):
            raise ValueError("no such pulse")
        assert motion.owned() == {HOLD_LEFT: a}

    def test_failed_hold_claims_nothing(self, motion, a) -> None:
        with pytest.raises(KeyError), calling_as(a), motion.hold_started("x"):
            raise KeyError("unknown servo_id: 'x'")
        assert motion.owned() == {}

    def test_failed_stop_keeps_the_owner(self, motion, a, b) -> None:
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        with pytest.raises(ValueError), calling_as(b), motion.flipper_stopped(LEFT):
            raise ValueError("servo line gone")
        assert motion.owned() == {FLIPPER_LEFT: a}

    def test_failed_command_takes_over_from_a_closed_owner(self, motion, a, b) -> None:
        # A has already run its cleanup and will never stop this, so B must.
        with calling_as(a), motion.speed_set(LEFT, 0.5):
            pass
        a.closed = True
        with pytest.raises(ValueError), calling_as(b), motion.speed_set(LEFT, 0.6):
            raise ValueError("no such pulse")
        assert motion.owned() == {FLIPPER_LEFT: b}

    def test_failure_leaves_a_newer_claim_alone(self, motion, a, b) -> None:
        with pytest.raises(ValueError), calling_as(a), motion.speed_set(LEFT, 0.5):
            with calling_as(b), motion.speed_set(LEFT, 0.6):
                pass
            raise ValueError("no such pulse")
        assert motion.owned() == {FLIPPER_LEFT: b}

    async def test_cancelled_claim_stands_and_cancelled_release_is_undone(
        self, motion, a
    ) -> None:
        # Either way the part may be moving, so someone has to stay responsible.
        right = _Flipper("right")
        with calling_as(a), motion.speed_set(right, 0.5):
            pass
        entered = asyncio.Event()

        async def claims() -> None:
            with calling_as(a), motion.speed_set(LEFT, 0.5):
                entered.set()
                await asyncio.sleep(60)

        async def releases() -> None:
            with motion.flipper_stopped(right):
                entered.set()
                await asyncio.sleep(60)

        for body in (claims, releases):
            entered.clear()
            task = asyncio.create_task(body())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert motion.owned() == {FLIPPER_LEFT: a, Part(Kind.FLIPPER, "right"): a}


class _NoArgs(BaseModel):
    pass


class _HoldStopArgs(BaseModel):
    id: str | None = None


class _Rig:
    """A dispatcher whose stop verbs only record what they were asked to stop."""

    def __init__(self) -> None:
        self.motion = MotionOwners()
        self.dispatcher = Dispatcher()
        self.stopped: list[str] = []
        self.broken = False

        @self.dispatcher.verb("flipper.left.stop", args_model=_NoArgs)
        async def _flipper_stop(_args: _NoArgs) -> dict:
            with self.motion.flipper_stopped(LEFT):
                if self.broken:
                    raise ValueError("servo line gone")
                self.stopped.append("flipper left")
            return {}

        @self.dispatcher.verb("servo.hold.stop", args_model=_HoldStopArgs)
        async def _hold_stop(args: _HoldStopArgs) -> dict:
            with self.motion.hold_stopped(args.id):
                self.stopped.append(f"hold {args.id}")
            return {}


class TestStopOwned:
    async def test_stops_only_what_the_owner_owns(self, a, b) -> None:
        rig = _Rig()
        with calling_as(a), rig.motion.speed_set(LEFT, 0.5):
            pass
        with calling_as(a), rig.motion.hold_started("right"):
            pass
        with calling_as(b), rig.motion.hold_started("m3"):
            pass
        await rig.motion.stop_owned(a, rig.dispatcher)
        assert sorted(rig.stopped) == ["flipper left", "hold right"]
        assert rig.motion.owned() == {HOLD_M3: b}

    async def test_part_taken_over_during_cleanup_is_left_alone(self, a, b) -> None:
        # The stop tasks start a tick after the list of parts is built. A
        # takeover landing in that gap must win.
        rig = _Rig()
        with calling_as(a), rig.motion.speed_set(LEFT, 0.5):
            pass

        async def takeover() -> None:
            with calling_as(b), rig.motion.speed_set(LEFT, 0.6):
                pass

        await asyncio.gather(rig.motion.stop_owned(a, rig.dispatcher), takeover())
        assert rig.stopped == []
        assert rig.motion.owned() == {FLIPPER_LEFT: b}

    async def test_failed_stop_is_logged_and_not_retried(self, a, caplog) -> None:
        # The cleanup runs with no owner, so the failed stop leaves the part to
        # nobody and a second pass has nothing to retry.
        rig = _Rig()
        with calling_as(a), rig.motion.speed_set(LEFT, 0.5):
            pass
        a.closed = True
        rig.broken = True
        await rig.motion.stop_owned(a, rig.dispatcher)
        assert "failed to stop flipper left for a" in caplog.text
        assert rig.motion.owned() == {}

    @pytest.mark.parametrize("kind", list(Kind))
    def test_every_kind_has_a_stop_command(self, kind) -> None:
        verb, args = Part(kind, "left").stop_command()
        assert verb.endswith(".stop")
        assert isinstance(args, dict)
