"""mDNS/Bonjour service advertisement.

Advertises the running host as ``_rsc-host._tcp.local`` so LAN clients can
discover every RSC on the network without knowing IPs or hostnames. The
service instance name defaults to the machine's hostname (``Shiny``,
``Pinky``, …) which doubles as the robot's identity.

TXT records exposed:

  * ``robot_name`` — the friendly name (hostname)
  * ``version``    — daemon version string
  * ``tls``        — 'true' | 'false'
  * ``proto``      — 'ws' | 'wss'

Uses ``zeroconf.asyncio.AsyncZeroconf`` — the sync ``Zeroconf`` class blocks
on ``asyncio.run_coroutine_threadsafe`` when called from inside a running
event loop, which is exactly our situation.

Discovery is *opportunistic*. If zeroconf fails to bind or the network stack
is misbehaving, the daemon logs a warning and carries on — clients that know
the hostname or IP still reach the service normally.
"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from rsc_host import __version__

log = logging.getLogger(__name__)

SERVICE_TYPE = "_rsc-host._tcp.local."


@dataclass(frozen=True, slots=True)
class DiscoveryInfo:
    """What gets published on the wire."""

    robot_name: str
    port: int
    tls: bool

    @property
    def proto(self) -> str:
        return "wss" if self.tls else "ws"

    def to_txt(self) -> dict[bytes, bytes]:
        """TXT records as zeroconf expects — bytes/bytes."""
        return {
            b"robot_name": self.robot_name.encode("utf-8"),
            b"version":    __version__.encode("utf-8"),
            b"tls":        (b"true" if self.tls else b"false"),
            b"proto":      self.proto.encode("utf-8"),
        }


def resolve_robot_name() -> str:
    """Robot name = short hostname. 'Shiny', 'Pinky', 'rsc-01', …

    Falls back to ``'rsc'`` if hostname lookup fails.
    """
    try:
        return socket.gethostname().split(".", 1)[0] or "rsc"
    except Exception:
        return "rsc"


def _resolve_local_ip() -> str | None:
    """Best-effort primary LAN IP. Returns None if no route is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


class ServiceAdvertiser:
    """Wraps AsyncZeroconf's registration lifecycle.

    Lazy-imports zeroconf so the dependency stays optional; the daemon
    continues normally if it's missing or fails at runtime.
    """

    def __init__(self, info: DiscoveryInfo) -> None:
        self._info = info
        self._zc = None  # zeroconf.asyncio.AsyncZeroconf | None
        self._service = None  # zeroconf.ServiceInfo | None

    async def start(self) -> None:
        try:
            from zeroconf import IPVersion, ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            log.warning(
                "zeroconf not installed; skipping LAN discovery. "
                "Install with: pip install zeroconf"
            )
            return

        ip = _resolve_local_ip()
        if ip is None:
            log.warning(
                "cannot determine local IP; skipping LAN discovery. "
                "Clients can still connect by hostname or explicit IP."
            )
            return

        instance = f"{self._info.robot_name}.{SERVICE_TYPE}"
        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._service = ServiceInfo(
            type_=SERVICE_TYPE,
            name=instance,
            addresses=[socket.inet_aton(ip)],
            port=self._info.port,
            properties=self._info.to_txt(),
            server=f"{self._info.robot_name}.local.",
        )
        try:
            await self._zc.async_register_service(self._service)
            log.info(
                "advertising as %s on %s:%d (%s)",
                instance, ip, self._info.port, self._info.proto,
            )
        except Exception:
            log.exception("zeroconf register_service failed; discovery disabled")
            self._service = None
            try:
                await self._zc.async_close()
            finally:
                self._zc = None

    async def stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._service is not None:
                await self._zc.async_unregister_service(self._service)
        except Exception:
            log.exception("zeroconf unregister failed; closing anyway")
        finally:
            try:
                await self._zc.async_close()
            except Exception:
                log.exception("zeroconf close failed")
            self._zc = None
            self._service = None
            log.info("LAN discovery advertisement stopped")