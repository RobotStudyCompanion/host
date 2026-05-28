"""rsc_host.hal — hardware abstraction layer.

Peripherals import the abstract bases from this package; concrete backends
(:mod:`rsc_host.hal.fake`, and later :mod:`rsc_host.hal.pi`) implement them.
"""
from __future__ import annotations

from rsc_host.hal.base import (
    AudioBackend,
    AudioCallback,
    Backend,
    GpioCallback,
    GpioInputBackend,
    GpioPwmBackend,
    RingBackend,
    SerialBackend,
    ServoBackend,
)
from rsc_host.hal.types import Colour, Edge, GpioEdge

__all__ = [
    # Lifecycle
    "Backend",
    # Peripheral type bases
    "ServoBackend",
    "RingBackend",
    "GpioInputBackend",
    "GpioPwmBackend",
    "SerialBackend",
    "AudioBackend",
    # Callbacks
    "GpioCallback",
    "AudioCallback",
    # Data types
    "Colour",
    "Edge",
    "GpioEdge",
]
