"""Validation for the free-form ``params`` of the ring and the button LED.

These arrive as an untyped dict and reach a background task that nobody awaits,
so a bad value must be turned away before the running pattern is touched. A
failure raises ``ValueError``, which the dispatcher reports as ``INVALID_ARGS``.

A field is checked whenever it is present, including in modes that don't use it.
Fields that are absent keep each mode's own default.
"""
from __future__ import annotations

from typing import Any

#: The floor exists so a tiny period can't become a loop that starves the event
#: loop; the ceiling, because nobody can tell a slower pattern from a stuck one.
PERIOD_MS_MIN = 50
PERIOD_MS_MAX = 60_000

COLOUR_MIN = 0
COLOUR_MAX = 255

DUTY_MIN = 0.0
DUTY_MAX = 1.0


def _check_range(name: str, value: Any, low: float, high: float) -> None:
    # bool is an int to Python, so it needs turning away by name.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    # json.loads accepts NaN and Infinity. Both fail this: NaN compares false to
    # everything, and infinity is beyond any limit.
    if not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}], got {value!r}")


def check_period_ms(params: dict[str, Any]) -> None:
    if "period_ms" in params:
        _check_range("period_ms", params["period_ms"], PERIOD_MS_MIN, PERIOD_MS_MAX)


def check_duty(params: dict[str, Any]) -> None:
    if "duty" in params:
        _check_range("duty", params["duty"], DUTY_MIN, DUTY_MAX)


def check_colour(params: dict[str, Any]) -> None:
    """Check whichever of ``r``, ``g``, ``b`` and ``rgb`` are present.

    A partial ``r``/``g``/``b`` is allowed, because the mode then falls back to
    its default colour, but any part that is present must still be valid.
    """
    for name in ("r", "g", "b"):
        if name in params:
            _check_range(name, params[name], COLOUR_MIN, COLOUR_MAX)
    if "rgb" in params:
        rgb = params["rgb"]
        if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
            raise ValueError(f"rgb must be a list of exactly three values, got {rgb!r}")
        for name, value in zip("rgb", rgb, strict=True):
            _check_range(f"rgb {name}", value, COLOUR_MIN, COLOUR_MAX)
