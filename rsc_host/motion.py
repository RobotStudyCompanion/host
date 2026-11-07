"""Which control connection answers for each moving part.

Flippers and servo holds carry on until told otherwise, so something has to
tell them when the client driving them goes away: a flipper left turning in a
lab is a hazard, and a held servo is the loudest thing on the microphone.

Verb handlers record ownership here, and the server calls :meth:`stop_owned`
when a connection ends. Handlers learn who is calling from :data:`current_owner`
rather than an argument, which leaves the handler signature alone. A command
dispatched from inside the daemon has no owner, so what it starts belongs to
nobody and has to be cleaned up by whatever started it.

Rules, per part:

* Whoever last set a flipper to a non-zero speed, or last held a servo, owns it.
* A stop from any connection clears ownership.
* Closing stops only what that connection still owns, through the dispatcher,
  so the normal events reach the other clients.

Every ownership change is made synchronously, before the handler first awaits.
The peripheral's lock is then reached in the same order, so a stop and a start
racing from two connections can't end with a flipper moving and no owner.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple

from rsc_host.dispatch import Dispatcher
from rsc_host.protocol import Cmd

log = logging.getLogger(__name__)

FLIPPER = "flipper"
HOLD = "hold"


class Part(NamedTuple):
    """One thing that can be left running: a flipper, or a hold on a servo."""

    kind: str
    id: str

    def stop_command(self) -> tuple[str, dict[str, str]]:
        """The verb and args that stop this part."""
        if self.kind == FLIPPER:
            return f"flipper.{self.id}.stop", {}
        return "servo.hold.stop", {"id": self.id}


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
    """Ownership table for one daemon."""

    def __init__(self) -> None:
        self._owners: dict[Part, Owner] = {}

    def owner_of(self, part: Part) -> Owner | None:
        return self._owners.get(part)

    def parts(self, kind: str) -> list[Part]:
        """Every owned part of ``kind``."""
        return [part for part in self._owners if part.kind == kind]

    @contextmanager
    def claiming(self, *parts: Part) -> Generator[None, None, None]:
        """The calling connection owns ``parts`` from now on, unless the command fails.

        Recorded up front so a connection closing mid-command still finds the
        part it was starting. Only an ordinary exception undoes the claim: a
        cancelled command may already have reached the hardware (cancellation
        lands at any await, including the event publish after the servo moved),
        so the claim has to stand and the cleanup will stop it.
        """
        owner = current_owner.get()
        previous = self._assign(parts, owner)
        try:
            yield
        except Exception:
            self._restore(previous, owner)
            raise

    @contextmanager
    def releasing(self, *parts: Part) -> Generator[None, None, None]:
        """Clear ownership of ``parts`` for a stop, unless the stop doesn't finish.

        A stop that failed or was cancelled may have left the part running, so
        here cancellation restores the owner too. If the part had in fact
        stopped, the worst outcome is one redundant stop later.
        """
        previous = self._assign(parts, None)
        try:
            yield
        except BaseException:
            self._restore(previous, None)
            raise

    async def stop_owned(self, owner: Owner, dispatcher: Dispatcher) -> None:
        """Stop everything ``owner`` still owns. One failure doesn't affect the rest."""
        parts = [part for part, who in self._owners.items() if who is owner]
        if not parts:
            return
        log.info(
            "connection %s closed: stopping %s",
            owner.name,
            ", ".join(f"{part.kind} {part.id}" for part in parts),
        )
        await asyncio.gather(*(self._stop_part(part, owner, dispatcher) for part in parts))

    async def _stop_part(self, part: Part, owner: Owner, dispatcher: Dispatcher) -> None:
        # Checked here, not when the list was built: another connection may have
        # taken this part over since. Nothing awaits between this check and the
        # stop handler clearing ownership, so the answer can't go stale.
        if self._owners.get(part) is not owner:
            return
        verb, args = part.stop_command()
        try:
            ack = await dispatcher.dispatch(
                Cmd(id=f"disconnect-{part.kind}-{part.id}", verb=verb, args=args)
            )
        except Exception:
            log.exception("failed to stop %s %s for %s", part.kind, part.id, owner.name)
            return
        if not ack.ok:
            log.error(
                "failed to stop %s %s for %s: %s: %s",
                part.kind, part.id, owner.name, ack.code, ack.message,
            )

    def _assign(self, parts: Iterable[Part], owner: Owner | None) -> dict[Part, Owner | None]:
        previous = {part: self._owners.get(part) for part in parts}
        for part in previous:
            self._set(part, owner)
        return previous

    def _restore(self, previous: dict[Part, Owner | None], installed: Owner | None) -> None:
        for part, who in previous.items():
            # Someone newer has spoken for this part since; theirs stands.
            if self._owners.get(part) is not installed:
                continue
            # A previous owner that has closed has already run its cleanup and
            # will never stop this. The connection whose command failed takes it.
            self._set(part, current_owner.get() if who is not None and who.closed else who)

    def _set(self, part: Part, owner: Owner | None) -> None:
        if owner is None:
            self._owners.pop(part, None)
        else:
            self._owners[part] = owner
