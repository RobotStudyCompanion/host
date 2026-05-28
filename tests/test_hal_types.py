"""HAL data type invariants: Colour validation/packing, Edge enum, GpioEdge."""
from __future__ import annotations

import pytest

from rsc_host.hal.types import Colour, Edge, GpioEdge


class TestColour:
    def test_construction_valid(self) -> None:
        c = Colour(255, 128, 0)
        assert (c.r, c.g, c.b) == (255, 128, 0)

    def test_construction_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            Colour(-1, 0, 0)

    def test_construction_rejects_over_255(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            Colour(0, 256, 0)

    def test_pack_and_unpack(self) -> None:
        c = Colour(0xAB, 0xCD, 0xEF)
        assert c.as_int() == 0xABCDEF
        assert Colour.from_int(0xABCDEF) == c

    def test_pack_unpack_extremes(self) -> None:
        assert Colour.black().as_int() == 0x000000
        assert Colour.white().as_int() == 0xFFFFFF

    def test_frozen(self) -> None:
        c = Colour(1, 2, 3)
        with pytest.raises((AttributeError, Exception)):
            c.r = 99  # type: ignore[misc]

    def test_hashable(self) -> None:
        # Frozen + slots means Colour is hashable — usable as dict keys / set members.
        s = {Colour(1, 2, 3), Colour(1, 2, 3), Colour(4, 5, 6)}
        assert len(s) == 2


class TestEdge:
    def test_string_values(self) -> None:
        assert Edge.RISING.value == "rising"
        assert Edge.FALLING.value == "falling"


class TestGpioEdge:
    def test_construction(self) -> None:
        e = GpioEdge(pin=23, edge=Edge.RISING, timestamp_ns=12345)
        assert e.pin == 23
        assert e.edge == Edge.RISING
        assert e.timestamp_ns == 12345

    def test_frozen(self) -> None:
        e = GpioEdge(pin=23, edge=Edge.RISING, timestamp_ns=12345)
        with pytest.raises((AttributeError, Exception)):
            e.pin = 24  # type: ignore[misc]
