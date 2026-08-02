"""Discovery: DiscoveryInfo, TXT records, robot_name resolution, graceful failure."""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from rsc_host.discovery import (
    DiscoveryInfo,
    ServiceAdvertiser,
    _resolve_local_ip,
    resolve_robot_name,
)


class TestDiscoveryInfo:
    def test_proto_ws_when_tls_off(self) -> None:
        info = DiscoveryInfo(robot_name="Shiny", port=8765, tls=False)
        assert info.proto == "ws"

    def test_proto_wss_when_tls_on(self) -> None:
        info = DiscoveryInfo(robot_name="Shiny", port=8765, tls=True)
        assert info.proto == "wss"

    def test_txt_records_bytes(self) -> None:
        info = DiscoveryInfo(robot_name="Shiny", port=8765, tls=False)
        txt = info.to_txt()
        assert txt[b"robot_name"] == b"Shiny"
        assert txt[b"proto"] == b"ws"
        assert txt[b"tls"] == b"false"
        # Version comes from the package; just check it's non-empty bytes.
        assert isinstance(txt[b"version"], bytes)
        assert len(txt[b"version"]) > 0

    def test_txt_tls_true_encoded(self) -> None:
        info = DiscoveryInfo(robot_name="X", port=1, tls=True)
        assert info.to_txt()[b"tls"] == b"true"

    def test_unicode_robot_name(self) -> None:
        # Some students will pick fun names — verify UTF-8 encoding survives.
        info = DiscoveryInfo(robot_name="Röbot-🤖", port=8765, tls=False)
        assert info.to_txt()[b"robot_name"] == "Röbot-🤖".encode("utf-8")


class TestRobotNameResolution:
    def test_uses_hostname(self) -> None:
        with patch("socket.gethostname", return_value="Shiny"):
            assert resolve_robot_name() == "Shiny"

    def test_strips_fqdn_domain(self) -> None:
        with patch("socket.gethostname", return_value="Shiny.local"):
            assert resolve_robot_name() == "Shiny"

    def test_falls_back_when_hostname_empty(self) -> None:
        with patch("socket.gethostname", return_value=""):
            assert resolve_robot_name() == "rsc"

    def test_falls_back_when_hostname_raises(self) -> None:
        with patch("socket.gethostname", side_effect=OSError("no")):
            assert resolve_robot_name() == "rsc"


class TestLocalIpResolution:
    def test_returns_string_on_normal_network(self) -> None:
        # We can't guarantee network in every CI, but if there IS a route,
        # the result should be a plausible IPv4 string.
        ip = _resolve_local_ip()
        if ip is not None:
            parts = ip.split(".")
            assert len(parts) == 4
            for part in parts:
                assert 0 <= int(part) <= 255

    def test_returns_none_when_socket_fails(self) -> None:
        # No routable network → returns None, doesn't raise.
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.connect.side_effect = OSError()
            assert _resolve_local_ip() is None


class TestAdvertiserGracefulDegradation:
    async def test_missing_zeroconf_logs_and_continues(self, caplog) -> None:
        # If zeroconf isn't importable, start() must not raise.
        advertiser = ServiceAdvertiser(
            DiscoveryInfo(robot_name="Shiny", port=8765, tls=False)
        )
        with patch.dict("sys.modules", {"zeroconf": None}):
            # Force ImportError on `from zeroconf import ...`
            import importlib
            with patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **k: (
                    (_ for _ in ()).throw(ImportError())
                    if name == "zeroconf"
                    else importlib.__import__(name, *a, **k)
                ),
            ):
                await advertiser.start()
        # stop() must also be safe when start never succeeded.
        await advertiser.stop()

    async def test_no_local_ip_skips_registration(self) -> None:
        advertiser = ServiceAdvertiser(
            DiscoveryInfo(robot_name="Shiny", port=8765, tls=False)
        )
        with patch("rsc_host.discovery._resolve_local_ip", return_value=None):
            await advertiser.start()
        # Advertiser should have skipped register; stop() still safe.
        await advertiser.stop()

    async def test_stop_without_start_is_safe(self) -> None:
        advertiser = ServiceAdvertiser(
            DiscoveryInfo(robot_name="Shiny", port=8765, tls=False)
        )
        await advertiser.stop()  # no-op, no raise
