"""Validation and parsing for the free-form ``params`` of the ring and button LED.

These arrive as an untyped dict and reach a background task that nobody awaits,
so a bad value must be turned away before the running pattern is touched. A
failure raises ``ValueError``, which the dispatcher reports as ``INVALID_ARGS``.

A field is checked whenever it is present, including in modes that don't use it.
Fields that are absent keep each mode's own default. The dict is only checked,
never rewritten, so the modes read exactly what the client sent.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any

from pydantic import Field, Strict, TypeAdapter, ValidationError

from rsc_host.hal.types import Colour

# Strict turns away bool and strings, which lax mode would coerce. NaN and
# infinity are refused outright: json.loads accepts both.
#
# The period's floor exists so a tiny period can't become a loop that starves
# the event loop; its ceiling, because nobody can tell a slower pattern from a
# stuck one.
_Period = Annotated[float, Strict(), Field(ge=50, le=60_000, allow_inf_nan=False)]
_Duty = Annotated[float, Strict(), Field(ge=0.0, le=1.0, allow_inf_nan=False)]
_Channel = Annotated[float, Strict(), Field(ge=0, le=255, allow_inf_nan=False)]

_FIELDS: dict[str, TypeAdapter[Any]] = {
    "period_ms": TypeAdapter(_Period),
    "duty": TypeAdapter(_Duty),
    "r": TypeAdapter(_Channel),
    "g": TypeAdapter(_Channel),
    "b": TypeAdapter(_Channel),
    "rgb": TypeAdapter(tuple[_Channel, _Channel, _Channel]),
}

# How pydantic reports a list that is too short or too long. Only ``rgb``, the
# one fixed-length field, can raise these.
_WRONG_LENGTH = {"missing", "too_long"}

#: The fields each peripheral checks.
RING_FIELDS = ("period_ms", "r", "g", "b", "rgb")
LED_FIELDS = ("period_ms", "duty")


def check(params: dict[str, Any], fields: Iterable[str]) -> None:
    """Raise ``ValueError`` for the first of ``fields`` that is present and bad."""
    for name in fields:
        if name not in params:
            continue
        value = params[name]
        try:
            _FIELDS[name].validate_python(value)
        except ValidationError as exc:
            # Re-raised short: pydantic's own text runs to several lines and a URL.
            error = exc.errors()[0]
            if error["type"] in _WRONG_LENGTH:
                raise ValueError(
                    f"{name}: must be a list of exactly 3 numbers, got {value!r}"
                ) from None
            where = "".join(f"[{part}]" for part in error["loc"])
            raise ValueError(f"{name}{where}: {error['msg']}, got {value!r}") from None


def parse_colour(params: dict[str, Any], default: Colour) -> Colour:
    """The colour in ``params``, given as ``rgb: [r, g, b]`` or as ``r``, ``g``, ``b``.

    ``rgb`` wins when both are given. A partial ``r``/``g``/``b`` falls back to
    ``default``. Fractions truncate. Assumes :func:`check` has already passed.
    """
    if "rgb" in params:
        r, g, b = params["rgb"]
    elif "r" in params and "g" in params and "b" in params:
        r, g, b = params["r"], params["g"], params["b"]
    else:
        return default
    return Colour(int(r), int(g), int(b))
