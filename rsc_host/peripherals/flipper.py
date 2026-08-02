"""Flipper (continuous-rotation servo) wrapper with async ramp trajectories.

Wraps a :class:`~rsc_host.hal.base.ServoBackend` with:

* Human-friendly speed range **−1.0..+1.0** (validated).
* Optional **ramping** — interpolate from current speed to target across a
  time window at ~100 Hz, for quieter starts/stops.
* An **M3 soft-disable** flag: the third flipper's PWM line exists on the HAT
  but the chassis only ships with two flippers today. M3 accepts commands
  and returns success, but doesn't touch the HAL until enabled.

Events emitted on the bus:

* ``flipper.<id>.state`` — ``{"speed": <float>}`` — fired after every
  successful ``set_speed``. Includes ramping intermediate values so clients
  can visualise the trajectory.
"""
from __future__ import annotations

import asyncio
import logging

from rsc_host.events import EventBus
from rsc_host.hal.base import ServoBackend
from rsc_host.protocol import Event

log = logging.getLogger(__name__)

# Ramp control loop runs at this rate. Fine enough for smooth ramps; coarse
# enough not to hog the event loop on a Pi.
_RAMP_HZ = 100.0
_RAMP_PERIOD = 1.0 / _RAMP_HZ


class Flipper:
    """One flipper servo (left / right / m3).

    Args:
        servo_id: Logical name — 'left', 'right', 'm3'.
        backend:  Any :class:`ServoBackend` (fake or Pi).
        bus:      EventBus to publish ``flipper.<id>.state`` on.
        enabled:  If ``False``, commands succeed but don't reach the HAL.
                  Used for M3 until the third servo is fitted.
    """

    def __init__(
        self,
        servo_id: str,
        backend: ServoBackend,
        bus: EventBus,
        *,
        enabled: bool = True,
    ) -> None:
        self.id = servo_id
        self._backend = backend
        self._bus = bus
        self._enabled = enabled
        self._current_speed = 0.0
        # Serialise ramps for this flipper: a new set_speed cancels an in-flight
        # ramp cleanly rather than fighting it. One ramp task per flipper.
        self._ramp_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current_speed(self) -> float:
        return self._current_speed

    async def set_speed(self, target: float, ramp_ms: int = 0) -> float:
        """Set the flipper to ``target`` speed (−1.0..+1.0), optionally ramped.

        Returns the final speed that was set (equal to ``target`` on success).
        """
        if not -1.0 <= target <= 1.0:
            raise ValueError(
                f"speed must be in [-1.0, 1.0], got {target!r}"
            )
        if ramp_ms < 0:
            raise ValueError(f"ramp_ms must be >= 0, got {ramp_ms!r}")

        async with self._lock:
            # Cancel any in-flight ramp; last-call-wins semantics.
            if self._ramp_task is not None and not self._ramp_task.done():
                self._ramp_task.cancel()
                try:
                    await self._ramp_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._ramp_task = None

            if ramp_ms == 0:
                await self._apply(target)
                return target

            # Start the ramp task, don't await it — long ramps shouldn't block
            # the caller. Clients that want to know when the ramp completes
            # can subscribe to the ``flipper.<id>.state`` events.
            start = self._current_speed
            self._ramp_task = asyncio.create_task(
                self._ramp(start, target, ramp_ms),
                name=f"flipper-{self.id}-ramp",
            )
        return target

    async def stop(self) -> None:
        """Emergency stop: cancel any ramp and drive to 0 immediately."""
        async with self._lock:
            if self._ramp_task is not None and not self._ramp_task.done():
                self._ramp_task.cancel()
                try:
                    await self._ramp_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._ramp_task = None
            await self._apply(0.0)

    async def _ramp(self, start: float, target: float, ramp_ms: int) -> None:
        """Interpolate ``start`` → ``target`` over ``ramp_ms`` at _RAMP_HZ."""
        try:
            steps = max(1, int(ramp_ms / 1000.0 * _RAMP_HZ))
            for i in range(1, steps + 1):
                fraction = i / steps
                intermediate = start + (target - start) * fraction
                await self._apply(intermediate)
                if i < steps:
                    await asyncio.sleep(_RAMP_PERIOD)
        except asyncio.CancelledError:
            # Preempted by a new set_speed — the new call will apply its
            # own target. Don't emit a final event here; the new call will.
            raise

    async def _apply(self, speed: float) -> None:
        """Write speed to the HAL (if enabled) and publish an event."""
        self._current_speed = speed
        if self._enabled:
            await self._backend.set_speed(self.id, speed)
        await self._bus.publish(
            Event(
                topic=f"flipper.{self.id}.state",
                source="host",
                data={"speed": speed, "enabled": self._enabled},
            )
        )
