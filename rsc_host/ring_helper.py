"""Privileged NeoPixel helper — the only part of the RSC stack that runs as root.

Why this exists
---------------
Blinka's NeoPixel driver reaches the PWM0 peripheral and a DMA channel by
mmap'ing ``/dev/mem``. That needs ``CAP_SYS_RAWIO``, which no group grants and
which is equivalent to root in practice. The ring is wired to GPIO 12 and the
hardware is fixed, so moving it to SPI (and the ``spi`` group) is not an
option.

Rather than run the whole daemon as root for one peripheral, this helper owns
the ring and nothing else. It accepts frames over a unix socket and writes
them to the strip. It has no network access, parses no user data beyond fixed
length binary, and can be restarted independently if the DMA channel wedges.

Run it as its own systemd unit; see ``systemd/rsc-ring.service``.

Wire protocol
-------------
Length-prefixed binary, both directions. Deliberately not JSON: frames arrive
at up to ~60 Hz and the payload is already packed bytes.

    request:  [1 byte op][2 bytes length, big-endian][payload]
    response: [1 byte status][2 bytes length, big-endian][payload]

    op 0x01 FRAME       payload = 4 bytes per pixel, R G B W
    op 0x02 CLEAR       no payload
    op 0x03 PING        no payload
    op 0x04 BRIGHTNESS  payload = 1 byte, 0..255
    op 0x05 INFO        no payload; response payload is JSON

    status 0x00 OK      response payload as documented per op
    status 0x01 ERROR   response payload is a UTF-8 message

A short frame is padded with black; an over-long frame is truncated. Neither
is an error — a client that has the pixel count slightly wrong should still
light the ring rather than fail closed.
"""
from __future__ import annotations

import argparse
import asyncio
import grp
import json
import logging
import os
import signal
import struct
import sys

log = logging.getLogger("rsc_host.ring_helper")

OP_FRAME = 0x01
OP_CLEAR = 0x02
OP_PING = 0x03
OP_BRIGHTNESS = 0x04
OP_INFO = 0x05

STATUS_OK = 0x00
STATUS_ERROR = 0x01

_HEADER = struct.Struct(">BH")
_MAX_PAYLOAD = 0xFFFF

DEFAULT_SOCKET = "/run/rsc/ring.sock"


class RingDriver:
    """Thin wrapper over Blinka's NeoPixel, with the RSC's specifics baked in.

    GPIO 12 selects PWM0 in Blinka's backend. GPIO 21 would select PCM — which
    is the I2S clock the WM8960 codec uses — and would kill audio. The pin is
    configurable only so a different build can use it; do not change it on this
    chassis.
    """

    def __init__(self, pin: int = 12, pixels: int = 16, brightness: float = 0.3) -> None:
        self.pin = pin
        self.pixels = pixels
        self.brightness = brightness
        self._np = None

    def start(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError(
                "the NeoPixel driver needs root to mmap /dev/mem for DMA. "
                "Run this helper as root (see systemd/rsc-ring.service)."
            )
        import board
        import neopixel

        pin_obj = getattr(board, f"D{self.pin}", None)
        if pin_obj is None:
            raise RuntimeError(f"board has no pin D{self.pin}")

        self._np = neopixel.NeoPixel(
            pin_obj,
            self.pixels,
            brightness=self.brightness,
            auto_write=False,
            pixel_order=neopixel.GRBW,  # SKC6812 RGBW chain
        )
        self.clear()
        log.info(
            "ring driver ready (GPIO%d, %d pixels, brightness %.2f)",
            self.pin, self.pixels, self.brightness,
        )

    def frame(self, payload: bytes) -> None:
        if self._np is None:
            raise RuntimeError("ring driver not started")
        count = min(self.pixels, len(payload) // 4)
        for i in range(count):
            base = i * 4
            self._np[i] = (
                payload[base],
                payload[base + 1],
                payload[base + 2],
                payload[base + 3],
            )
        for i in range(count, self.pixels):
            self._np[i] = (0, 0, 0, 0)
        self._np.show()

    def clear(self) -> None:
        if self._np is None:
            return
        self._np.fill((0, 0, 0, 0))
        self._np.show()

    def set_brightness(self, value: float) -> None:
        self.brightness = max(0.0, min(1.0, value))
        if self._np is not None:
            self._np.brightness = self.brightness
            self._np.show()

    def info(self) -> dict:
        return {
            "pin": self.pin,
            "pixels": self.pixels,
            "brightness": round(self.brightness, 3),
            "driver": "neopixel/rpi_ws281x",
            "pixel_order": "GRBW",
        }

    def stop(self) -> None:
        if self._np is None:
            return
        try:
            self.clear()
            self._np.deinit()
        except Exception:
            log.exception("failed to release the ring cleanly")
        finally:
            self._np = None
            log.info("ring driver released")


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    driver: RingDriver,
) -> None:
    peer = writer.get_extra_info("peername") or "unix"
    log.info("client connected (%s)", peer)

    async def respond(status: int, payload: bytes = b"") -> None:
        writer.write(_HEADER.pack(status, len(payload)) + payload)
        await writer.drain()

    try:
        while True:
            try:
                header = await reader.readexactly(_HEADER.size)
            except asyncio.IncompleteReadError:
                return
            op, length = _HEADER.unpack(header)
            payload = await reader.readexactly(length) if length else b""

            try:
                if op == OP_FRAME:
                    driver.frame(payload)
                    await respond(STATUS_OK)
                elif op == OP_CLEAR:
                    driver.clear()
                    await respond(STATUS_OK)
                elif op == OP_PING:
                    await respond(STATUS_OK)
                elif op == OP_BRIGHTNESS:
                    if len(payload) != 1:
                        raise ValueError("BRIGHTNESS takes exactly one byte")
                    driver.set_brightness(payload[0] / 255.0)
                    await respond(STATUS_OK)
                elif op == OP_INFO:
                    await respond(STATUS_OK, json.dumps(driver.info()).encode())
                else:
                    raise ValueError(f"unknown op 0x{op:02x}")
            except Exception as exc:
                log.exception("op 0x%02x failed", op)
                await respond(STATUS_ERROR, str(exc).encode("utf-8")[:_MAX_PAYLOAD])
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        log.info("client disconnected (%s)", peer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _prepare_socket_path(path: str, group: str | None) -> None:
    """Remove a stale socket and make sure the parent directory exists."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)


def _secure_socket(path: str, group: str | None) -> None:
    """Restrict the socket to the owning group. Root writes, the daemon's group
    reads — nothing else on the box can drive the ring."""
    os.chmod(path, 0o660)
    if not group:
        return
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        log.warning("group %r not found; leaving socket owned by root", group)
        return
    os.chown(path, 0, gid)
    log.info("socket %s owned by root:%s mode 0660", path, group)


async def _serve(args: argparse.Namespace) -> int:
    driver = RingDriver(pin=args.gpio, pixels=args.pixels, brightness=args.brightness)
    try:
        driver.start()
    except Exception as exc:
        log.error("cannot initialise the ring: %s", exc)
        return 1

    _prepare_socket_path(args.socket, args.group)
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, driver), path=args.socket
    )
    _secure_socket(args.socket, args.group)
    log.info("listening on %s", args.socket)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        await shutdown.wait()
    finally:
        log.info("shutting down")
        server.close()
        await server.wait_closed()
        driver.stop()
        try:
            os.unlink(args.socket)
        except OSError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Privileged NeoPixel ring helper for the RSC host daemon"
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help=f"unix socket path (default: {DEFAULT_SOCKET})")
    parser.add_argument("--gpio", type=int, default=12,
                        help="BCM pin driving the ring (default: 12 = PWM0)")
    parser.add_argument("--pixels", type=int, default=16,
                        help="pixel count (default: 16)")
    parser.add_argument("--brightness", type=float, default=0.3,
                        help="global brightness 0.0-1.0 (default: 0.3)")
    parser.add_argument("--group", default="gpio",
                        help="group granted access to the socket (default: gpio)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
