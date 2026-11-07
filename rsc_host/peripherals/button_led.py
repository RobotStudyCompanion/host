"""Arcade button LED — GPIO-PWM output with a small pattern set.

Simpler than the ring: one PWM channel, so patterns are strictly time-varying
duty. Modes:

* ``off``     — duty 0
* ``on``      — duty 1 (or the ``duty`` param if given, default 1.0)
* ``pulse``   — sinusoidal 0..1 modulation
* ``breathe`` — asymmetric pulse (faster rise than fall) — subjectively nicer
                than a plain sine for the arcade button's rest state

Events emitted:

* ``button_led.state`` — ``{"mode": <name>, "duty": <float>}`` — after every mode change.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from typing import Any

from rsc_host.events import EventBus
from rsc_host.hal.base import GpioPwmBackend
from rsc_host.peripherals.params import LED_FIELDS, check
from rsc_host.protocol import Event

log = logging.getLogger(__name__)

_FRAME_PERIOD = 0.03  # ~33 Hz — smooth enough, gentle on the loop.


# ---- Modes ----
#
# A steady mode sets one duty and is done. A pattern runs as a background task
# until the next mode change cancels it. Every mode is in exactly one table.


async def _pulse(backend: GpioPwmBackend, pin: int, params: dict[str, Any]) -> None:
    period_s = float(params.get("period_ms", 2000)) / 1000.0
    t = 0.0
    while True:
        phase = (t % period_s) / period_s
        duty = 0.5 * (1.0 - math.cos(2.0 * math.pi * phase))
        await backend.set_duty(pin, duty)
        await asyncio.sleep(_FRAME_PERIOD)
        t += _FRAME_PERIOD


async def _breathe(backend: GpioPwmBackend, pin: int, params: dict[str, Any]) -> None:
    period_s = float(params.get("period_ms", 3000)) / 1000.0
    # Asymmetric: rise takes 40 % of period, fall 60 %. Feels "alive".
    rise_frac = 0.4
    rise_s = period_s * rise_frac
    fall_s = period_s - rise_s
    t = 0.0
    while True:
        phase = t % period_s
        if phase < rise_s:
            duty = phase / rise_s
        else:
            duty = 1.0 - (phase - rise_s) / fall_s
        duty = max(0.0, min(1.0, duty))
        await backend.set_duty(pin, duty)
        await asyncio.sleep(_FRAME_PERIOD)
        t += _FRAME_PERIOD


_STEADY: dict[str, Callable[[dict[str, Any]], float]] = {
    "off": lambda params: 0.0,
    "on": lambda params: float(params.get("duty", 1.0)),
}

_PATTERNS: dict[
    str, Callable[[GpioPwmBackend, int, dict[str, Any]], Coroutine[Any, Any, None]]
] = {
    "pulse": _pulse,
    "breathe": _breathe,
}


class ButtonLed:
    """Arcade button's PWM-driven LED on ``pin``."""

    def __init__(
        self,
        pin: int,
        backend: GpioPwmBackend,
        bus: EventBus,
    ) -> None:
        self._pin = pin
        self._backend = backend
        self._bus = bus
        self._task: asyncio.Task[None] | None = None
        self._current_mode: str = "off"
        self._lock = asyncio.Lock()

    @property
    def current_mode(self) -> str:
        return self._current_mode

    async def set_mode(self, mode: str, params: dict[str, Any] | None = None) -> None:
        """Switch to ``mode``.

        Raises ``KeyError`` for an unknown mode and ``ValueError`` for bad
        params, both before anything changes, so the running pattern survives.
        """
        if mode not in _STEADY and mode not in _PATTERNS:
            raise KeyError(
                f"unknown button LED mode: {mode!r}; "
                f"known: {', '.join([*_STEADY, *_PATTERNS])}"
            )
        params = params or {}
        check(params, LED_FIELDS)

        async with self._lock:
            await self._cancel_task()
            self._current_mode = mode

            if mode in _STEADY:
                duty = _STEADY[mode](params)
                await self._backend.set_duty(self._pin, duty)
            else:
                duty = 0.0
                self._task = asyncio.create_task(
                    _PATTERNS[mode](self._backend, self._pin, params),
                    name=f"button-led-{mode}",
                )

        await self._bus.publish(
            Event(
                topic="button_led.state",
                source="host",
                data={"mode": mode, "duty": duty},
            )
        )

    async def stop(self) -> None:
        async with self._lock:
            await self._cancel_task()
            await self._backend.set_duty(self._pin, 0.0)
            self._current_mode = "off"

    async def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
