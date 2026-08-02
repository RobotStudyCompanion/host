"""Peripheral wrappers around HAL backends.

Each peripheral in this package composes a :mod:`rsc_host.hal` backend with
semantic logic — ramping, mode animation, debouncing, protocol translation —
and publishes state onto the :class:`~rsc_host.events.EventBus`.

Peripherals are wired to the dispatcher, event bus, and HAL by
:func:`rsc_host.peripherals.registry.setup`, called from
:mod:`rsc_host.__main__` at startup.
"""
from __future__ import annotations
