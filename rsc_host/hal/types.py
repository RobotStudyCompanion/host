"""Shared HAL data types.

Plain dataclasses, stdlib only — pydantic stays at the wire-protocol layer.
Validation here covers structural invariants (e.g. 0..255 channel range), not
semantic correctness (that's the peripheral's job).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class Colour:
    """24-bit RGB colour. Each channel 0..255.

    Frozen so it can be hashed and shared safely across backends. Use
    :meth:`as_int` for packed-int APIs (rpi_ws281x takes 0xRRGGBB).
    """

    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for value, name in ((self.r, "r"), (self.g, "g"), (self.b, "b")):
            if not 0 <= value <= 255:
                raise ValueError(f"{name} channel out of range 0..255: {value}")

    def as_int(self) -> int:
        """Pack into 0xRRGGBB."""
        return (self.r << 16) | (self.g << 8) | self.b

    @classmethod
    def from_int(cls, packed: int) -> "Colour":
        """Unpack 0xRRGGBB into a Colour."""
        return cls((packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF)

    @classmethod
    def black(cls) -> "Colour":
        return cls(0, 0, 0)

    @classmethod
    def white(cls) -> "Colour":
        return cls(255, 255, 255)


class Edge(str, Enum):
    """Direction of a digital pin transition."""

    RISING = "rising"
    FALLING = "falling"


@dataclass(frozen=True, slots=True)
class GpioEdge:
    """A single edge event on a digital input pin.

    ``timestamp_ns`` is monotonic — useful for debouncing and ordering, not for
    wall-clock display.
    """

    pin: int
    edge: Edge
    timestamp_ns: int
