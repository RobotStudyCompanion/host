"""NeoPixel ring wrapper with a pluggable mode registry.

A **mode** is an async coroutine that repeatedly writes to the ring's staged
frame and calls ``ring.show()``. Modes register via the :func:`ring_mode`
decorator; new modes are one function each. The ring runs one mode at a time —
starting a new mode cancels the previous.

Modes shipped in this file:

* ``off``     — clear and hold
* ``solid``   — fill with a colour and hold
* ``pulse``   — sinusoidal brightness modulation
* ``spin``    — one lit pixel walks around the ring
* ``sweep``   — alias for spin (used by the legacy test script)

Adding a new mode later — say a rainbow — is::

    @ring_mode("rainbow")
    async def _rainbow(ring, backend, params):
        while True:
            # ... write frames ...
            await asyncio.sleep(0.03)

Events emitted:

* ``ring.mode`` — ``{"mode": <name>, "params": {...}}`` — after a mode change.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from rsc_host.errors import PeripheralUnavailableError
from rsc_host.events import EventBus
from rsc_host.hal.base import RingBackend
from rsc_host.hal.types import Colour
from rsc_host.protocol import Event

log = logging.getLogger(__name__)

# Mode signature: (backend, params) → coroutine that loops forever until cancelled.
ModeFn = Callable[[RingBackend, dict[str, Any]], Awaitable[None]]

_modes: dict[str, ModeFn] = {}


def ring_mode(name: str) -> Callable[[ModeFn], ModeFn]:
    """Register ``func`` as ring mode ``name``.

    Raises:
        ValueError: if ``name`` is already registered.
    """

    def decorator(func: ModeFn) -> ModeFn:
        if name in _modes:
            raise ValueError(f"ring mode already registered: {name!r}")
        _modes[name] = func
        log.debug("registered ring mode: %s", name)
        return func

    return decorator


def registered_modes() -> tuple[str, ...]:
    """All known mode names, sorted."""
    return tuple(sorted(_modes.keys()))


# ---- Built-in modes ----


def _parse_colour(params: dict[str, Any], default: Colour = Colour(255, 255, 255)) -> Colour:
    """Pull an RGB triple out of ``params``. Supports ``{r,g,b}`` or ``rgb: [r,g,b]``."""
    if "rgb" in params:
        r, g, b = params["rgb"]
    elif "r" in params and "g" in params and "b" in params:
        r, g, b = params["r"], params["g"], params["b"]
    else:
        return default
    return Colour(int(r), int(g), int(b))


@ring_mode("off")
async def _off(backend: RingBackend, params: dict[str, Any]) -> None:
    """All pixels off. Runs once and holds forever."""
    await backend.fill(Colour.black())
    await backend.show()
    # Sleep forever so the mode-task lifetime matches other modes;
    # cancellation is the exit path.
    await asyncio.Event().wait()


@ring_mode("solid")
async def _solid(backend: RingBackend, params: dict[str, Any]) -> None:
    """Fill with a single colour and hold."""
    colour = _parse_colour(params, default=Colour(255, 255, 255))
    await backend.fill(colour)
    await backend.show()
    await asyncio.Event().wait()


@ring_mode("pulse")
async def _pulse(backend: RingBackend, params: dict[str, Any]) -> None:
    """Sinusoidal brightness modulation of ``colour``.

    Params:
        colour params (r/g/b or rgb)
        period_ms: pulse period in ms (default 1500)
    """
    colour = _parse_colour(params, default=Colour(0, 80, 255))
    period_ms = int(params.get("period_ms", 1500))
    frame_period = 0.03  # ~33 Hz
    t = 0.0
    while True:
        phase = (t % (period_ms / 1000.0)) / (period_ms / 1000.0)
        # 0..1..0 across the period; sin gives smooth ends
        brightness = 0.5 * (1.0 - math.cos(2.0 * math.pi * phase))
        scaled = Colour(
            int(colour.r * brightness),
            int(colour.g * brightness),
            int(colour.b * brightness),
        )
        await backend.fill(scaled)
        await backend.show()
        await asyncio.sleep(frame_period)
        t += frame_period


@ring_mode("spin")
async def _spin(backend: RingBackend, params: dict[str, Any]) -> None:
    """One lit pixel walks around the ring.

    Params:
        colour params (r/g/b or rgb)
        period_ms: full-revolution time (default 1000)
    """
    colour = _parse_colour(params, default=Colour(0, 80, 255))
    period_ms = int(params.get("period_ms", 1000))
    n = backend.pixel_count
    step_period = (period_ms / 1000.0) / n
    prev = 0
    # Start dark
    await backend.fill(Colour.black())
    await backend.show()
    while True:
        for i in range(n):
            await backend.set_pixel(prev, Colour.black())
            await backend.set_pixel(i, colour)
            await backend.show()
            prev = i
            await asyncio.sleep(step_period)


@ring_mode("sweep")
async def _sweep(backend: RingBackend, params: dict[str, Any]) -> None:
    """Alias of ``spin`` — kept for parity with the legacy test script."""
    await _spin(backend, params)


# ---- Peripheral wrapper ----


class Ring:
    """NeoPixel ring peripheral.

    One mode runs at a time; :meth:`set_mode` cancels the previous. The mode
    task lives on the event loop and is cleaned up on :meth:`stop`.
    """

    def __init__(self, backend: RingBackend, bus: EventBus) -> None:
        self._backend = backend
        self._bus = bus
        self._task: asyncio.Task[None] | None = None
        self._current_mode: str | None = None
        self._current_params: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def current_mode(self) -> str | None:
        return self._current_mode

    async def set_mode(self, mode: str, params: dict[str, Any] | None = None) -> None:
        """Switch to ``mode``.

        Raises ``KeyError`` for an unknown mode, and
        :class:`~rsc_host.errors.PeripheralUnavailableError` when the ring has
        no privileged path to the hardware — better an explicit failure than a
        success Ack for pixels nobody will ever see.
        """
        if mode not in _modes:
            raise KeyError(
                f"unknown ring mode: {mode!r}; known: {registered_modes()}"
            )
        if not self._backend.available:
            status = self._backend.status()
            raise PeripheralUnavailableError(
                f"ring is not available: {status.get('reason') or 'unknown reason'}"
            )
        params = params or {}
        async with self._lock:
            await self._cancel_task()
            self._current_mode = mode
            self._current_params = dict(params)
            fn = _modes[mode]
            self._task = asyncio.create_task(
                self._run_mode(mode, fn, dict(params)),
                name=f"ring-mode-{mode}",
            )
        await self._bus.publish(
            Event(
                topic="ring.mode",
                source="host",
                data={"mode": mode, "params": params},
            )
        )

    async def _run_mode(self, name: str, fn: ModeFn, params: dict[str, Any]) -> None:
        """Run a mode coroutine, catching a hardware disappearance.

        If the helper dies mid-animation the mode raises on its next
        ``show()``. Without this wrapper that becomes an unretrieved task
        exception — logged by asyncio at an unhelpful moment, with no clue
        which mode it came from.
        """
        try:
            await fn(self._backend, params)
        except asyncio.CancelledError:
            raise
        except PeripheralUnavailableError as exc:
            log.warning("ring mode %r stopped: %s", name, exc)
        except Exception:
            log.exception("ring mode %r failed", name)

    async def stop(self) -> None:
        """Cancel the mode task and clear the ring."""
        async with self._lock:
            await self._cancel_task()
            try:
                await self._backend.fill(Colour.black())
                await self._backend.show()
            except PeripheralUnavailableError:
                pass  # nothing lit, nothing to clear
            self._current_mode = None

    async def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
