"""Exception taxonomy shared across the HAL, peripherals, and the dispatcher.

Lives at the package root with no intra-package imports, so every layer can
raise these without creating a cycle. :mod:`rsc_host.dispatch` maps them onto
:class:`~rsc_host.protocol.ErrorCode` values; anything else a handler raises
becomes ``INTERNAL_ERROR``.

The rule of thumb for which to raise:

* :class:`PeripheralBusyError` — the hardware is fine, someone else has it.
  Retrying later will work.
* :class:`PeripheralUnavailableError` — the hardware is absent, unprivileged,
  or failed to initialise. Retrying will not work until something changes on
  the host. The ring raises this when the privileged helper is not reachable.
* :class:`CalibrationError` — a calibration value was rejected as physically
  implausible (e.g. a servo null outside the 900–2100 µs pulse window).
"""
from __future__ import annotations


class RscError(Exception):
    """Base for every error this daemon raises deliberately."""


class PeripheralBusyError(RscError):
    """Peripheral is in use and cannot accept an overlapping request."""


class PeripheralUnavailableError(RscError):
    """Peripheral is unreachable — hardware absent, driver missing, or the
    process lacks the privilege it needs.

    Raised rather than swallowed so the client sees ``PERIPHERAL_UNAVAILABLE``
    instead of a success Ack for something that never happened. The daemon
    still boots with an unavailable peripheral; only that peripheral's verbs
    fail.
    """


class CalibrationError(RscError, ValueError):
    """A calibration value was rejected. Subclasses ValueError so existing
    ``except ValueError`` sites keep working."""
