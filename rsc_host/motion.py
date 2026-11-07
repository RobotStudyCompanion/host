"""Which control connection answers for each moving part.

Flippers and servo holds carry on until told otherwise, so something has to
tell them when the client driving them goes away: a flipper left turning in a
lab is a hazard, and a held servo is the loudest thing on the microphone.

Verb handlers wrap the hardware call in one of the policy methods below
(:meth:`MotionOwners.speed_set`, :meth:`~MotionOwners.flipper_stopped`,
:meth:`~MotionOwners.hold_started`, :meth:`~MotionOwners.hold_stopped`), and the
server calls :meth:`~MotionOwners.stop_owned` when a connection ends. Handlers
learn who is calling from :data:`current_owner` rather than an argument, which
leaves the handler signature alone. A command dispatched from inside the daemon
has no owner, so what it starts belongs to nobody and has to be cleaned up by
whatever started it.

Rules, per part:

* Whoever last set a flipper to a non-zero speed, or last held a servo, owns it.
* A stop from any connection clears ownership. A speed of zero is a stop, ramped
  or not: the ramp ends by itself.
* A flipper and a hold on the same servo are one physical output:

  * Any speed that reaches the servo ends its hold, so a speed command also
    clears the hold's owner. A disabled flipper (``m3``) never reaches the
    servo, so its commands leave the hold alone.
  * A hold replaces the flipper's pulses, so the holder also takes over a
    flipper someone owns. Otherwise the earlier owner closing would stop the
    flipper, and that stop would end the hold too.

* Closing stops only what that connection still owns, through the dispatcher,
  so the normal events reach the other clients.

Every ownership change is made synchronously, before the handler first awaits.
The peripheral's lock is then reached in the same order, so a stop and a start
racing from two connections can't end with a flipper moving and no owner.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple, assert_never

from rsc_host.dispatch import Dispatcher
from rsc_host.protocol import Cmd

if TYPE_CHECKING:
    from rsc_host.peripherals.flipper import Flipper

log = logging.getLogger(__name__)


class Kind(Enum):
    FLIPPER = "flipper"
    HOLD = "hold"


class Part(NamedTuple):
    """One thing that can be left running: a flipper, or a hold on a servo."""

    kind: Kind
    id: str

    def stop_command(self) -> tuple[str, dict[str, str]]:
        """The verb and args that stop this part."""
        match self.kind:
            case Kind.FLIPPER:
                return f"flipper.{self.id}.stop", {}
            case Kind.HOLD:
                return "servo.hold.stop", {"id": self.id}
            case _:
                assert_never(self.kind)


class Owner:
    """One control connection, for as long as it can answer for motion."""

    __slots__ = ("name", "closed")

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False


#: The connection whose command is running. Set by the server's receive loop;
#: None for anything dispatched from inside the daemon.
current_owner: ContextVar[Owner | None] = ContextVar("rsc_motion_owner", default=None)


class MotionOwners:
    """Ownership table for one daemon. Holds an entry only for owned parts."""

    def __init__(self) -> None:
        self._owners: dict[Part, Owner] = {}

    def owner_of(self, part: Part) -> Owner | None:
        return self._owners.get(part)

    def owned(self) -> dict[Part, Owner]:
        """A copy of the table."""
        return dict(self._owners)

    # ---- Policy: what each command does to ownership ----

    def speed_set(self, flipper: Flipper, speed: float) -> AbstractContextManager[None]:
        """Wrap a speed command: non-zero claims the flipper, zero is a stop."""
        part = Part(Kind.FLIPPER, flipper.id)
        holds = self._holds_ended_by(flipper)
        if speed == 0:
            return self._change(release=(part, *holds))
        return self._change(claim=(part,), release=holds)

    def flipper_stopped(self, flipper: Flipper) -> AbstractContextManager[None]:
        """Wrap a flipper stop."""
        return self.speed_set(flipper, 0.0)

    def hold_started(self, servo_id: str) -> AbstractContextManager[None]:
        """Wrap a hold: claims it, and the servo's flipper if someone owns that."""
        claim = [Part(Kind.HOLD, servo_id)]
        flipper = Part(Kind.FLIPPER, servo_id)
        if flipper in self._owners:
            claim.append(flipper)
        return self._change(claim=claim)

    def hold_stopped(self, servo_id: str | None) -> AbstractContextManager[None]:
        """Wrap a hold release: one servo's, or every hold if ``servo_id`` is empty."""
        if servo_id:
            release = [Part(Kind.HOLD, servo_id)]
        else:
            release = [part for part in self._owners if part.kind is Kind.HOLD]
        return self._change(release=release)

    @staticmethod
    def _holds_ended_by(flipper: Flipper) -> tuple[Part, ...]:
        return (Part(Kind.HOLD, flipper.id),) if flipper.enabled else ()

    # ---- Disconnect ----

    async def stop_owned(self, owner: Owner, dispatcher: Dispatcher) -> None:
        """Stop everything ``owner`` still owns. One failure doesn't affect the rest.

        A stop that fails here is logged and not retried: the cleanup runs
        without an owner, so the failed stop's restore leaves the part to nobody.
        """
        parts = [part for part, who in self._owners.items() if who is owner]
        if not parts:
            return
        log.info(
            "connection %s closed: stopping %s",
            owner.name,
            ", ".join(f"{part.kind.value} {part.id}" for part in parts),
        )
        await asyncio.gather(*(self._stop_part(part, owner, dispatcher) for part in parts))

    async def _stop_part(self, part: Part, owner: Owner, dispatcher: Dispatcher) -> None:
        # Checked here, not when the list was built: another connection may have
        # taken this part over since. Nothing awaits between this check and the
        # stop handler clearing ownership, so the answer can't go stale.
        if self._owners.get(part) is not owner:
            return
        verb, args = part.stop_command()
        kind = part.kind.value
        try:
            ack = await dispatcher.dispatch(
                Cmd(id=f"disconnect-{kind}-{part.id}", verb=verb, args=args)
            )
        except Exception:
            log.exception("failed to stop %s %s for %s", kind, part.id, owner.name)
            return
        if not ack.ok:
            log.error(
                "failed to stop %s %s for %s: %s: %s",
                kind, part.id, owner.name, ack.code, ack.message,
            )

    # ---- Table changes ----

    @contextmanager
    def _change(
        self, *, claim: Iterable[Part] = (), release: Iterable[Part] = ()
    ) -> Iterator[None]:
        """Give ``claim`` to the calling connection and ``release`` to nobody.

        Applied up front, so a connection closing mid-command still finds the
        part it was starting. A command from inside the daemon has no owner, so
        its claim also leaves the part to nobody.

        Undone if the command fails, with a difference on cancellation:

        * A cancelled claim stands. The command may already have reached the
          hardware (cancellation lands at any await, including the event publish
          after the servo moved), and the cleanup will stop it.
        * A cancelled release is undone. The stop may not have happened, and if
          it did, the worst outcome is one redundant stop later.
        """
        owner = current_owner.get()
        claimed = self._assign(claim, owner)
        released = self._assign(release, None)
        try:
            yield
        except Exception:
            self._restore(claimed, installed=owner, fallback=owner)
            self._restore(released, installed=None, fallback=owner)
            raise
        except BaseException:
            self._restore(released, installed=None, fallback=owner)
            raise

    def _assign(self, parts: Iterable[Part], owner: Owner | None) -> dict[Part, Owner | None]:
        previous = {part: self._owners.get(part) for part in parts}
        for part in previous:
            self._set(part, owner)
        return previous

    def _restore(
        self,
        previous: dict[Part, Owner | None],
        *,
        installed: Owner | None,
        fallback: Owner | None,
    ) -> None:
        """Put back ``previous`` where the table still shows ``installed``.

        A previous owner that has closed has already run its cleanup and will
        never stop the part, so ``fallback`` (the connection whose command
        failed) takes it instead.
        """
        for part, who in previous.items():
            # Someone newer has spoken for this part since; theirs stands.
            if self._owners.get(part) is not installed:
                continue
            self._set(part, fallback if who is not None and who.closed else who)

    def _set(self, part: Part, owner: Owner | None) -> None:
        """Record ``owner`` for ``part``. ``None`` is nobody: the entry goes."""
        if owner is None:
            self._owners.pop(part, None)
        else:
            self._owners[part] = owner
