"""End-to-end integration for /audio/out and /audio/in path handlers.

Boots a real Server, wires up the audio handlers against a FakeAudio backend,
connects clients, streams bytes both directions.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import websockets

from rsc_host.audio_endpoints import build_audio_in_handler, build_audio_out_handler
from rsc_host.auth import TokenAuth
from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeAudio
from rsc_host.peripherals.audio import Audio
from rsc_host.server import Server


TOKEN = "audio-test-token"


@pytest.fixture
async def rig() -> AsyncIterator[tuple[Server, Audio, FakeAudio, int]]:
    backend = FakeAudio()
    await backend.start()
    dispatcher = Dispatcher()
    bus = EventBus()
    auth = TokenAuth(TOKEN)
    audio = Audio(backend, bus)

    server = Server(dispatcher, bus, auth, bind="127.0.0.1", port=0)
    server.add_path_handler("/audio/out", build_audio_out_handler(audio))
    server.add_path_handler("/audio/in", build_audio_in_handler(audio))
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        yield server, audio, backend, port
    finally:
        await audio.stop_capture()
        await audio.stop_play()
        await backend.stop()
        await server.stop()


async def _connect(port: int, path: str, token: str = TOKEN):
    return await websockets.connect(
        f"ws://127.0.0.1:{port}{path}",
        subprotocols=["bearer", token],  # type: ignore[list-item]
    )


def _make_wav(pcm: bytes, samplerate: int = 16000, channels: int = 1) -> bytes:
    """Build a canonical RIFF/WAVE PCM file with the given raw samples."""
    import struct

    bits = 16
    byte_rate = samplerate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    data_size = len(pcm)
    riff_size = 36 + data_size
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, samplerate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_size)
        + pcm
    )


class TestAudioOut:
    async def test_wav_upload_streams_to_backend(self, rig) -> None:
        _, _, backend, port = rig
        pcm = b"\x00\x01" * 100  # 100 s16le samples
        wav = _make_wav(pcm, samplerate=16000, channels=1)

        ws = await _connect(port, "/audio/out")
        try:
            await ws.send(wav)
        finally:
            await ws.close()

        # Give the server a moment to finish the stream after client close.
        for _ in range(20):
            if backend.streamed():
                break
            await asyncio.sleep(0.05)

        streams = backend.streamed()
        assert len(streams) == 1
        assert streams[0]["samplerate"] == 16000
        assert streams[0]["channels"] == 1
        # All the PCM should have made it through (possibly in multiple chunks).
        combined = b"".join(streams[0]["chunks"])
        assert combined == pcm

    async def test_wav_split_across_frames(self, rig) -> None:
        _, _, backend, port = rig
        pcm = b"\xAA\xBB" * 200
        wav = _make_wav(pcm, samplerate=48000, channels=1)

        ws = await _connect(port, "/audio/out")
        try:
            # Split header + first PCM chunk across two frames deliberately.
            await ws.send(wav[:30])           # partial header
            await asyncio.sleep(0.02)
            await ws.send(wav[30:100])        # rest of header + some PCM
            await asyncio.sleep(0.02)
            await ws.send(wav[100:])          # remaining PCM
        finally:
            await ws.close()

        for _ in range(20):
            if backend.streamed():
                break
            await asyncio.sleep(0.05)

        streams = backend.streamed()
        assert len(streams) == 1
        assert streams[0]["samplerate"] == 48000
        combined = b"".join(streams[0]["chunks"])
        assert combined == pcm

    async def test_empty_upload_is_noop(self, rig) -> None:
        _, _, backend, port = rig
        ws = await _connect(port, "/audio/out")
        await ws.close()
        await asyncio.sleep(0.1)
        assert backend.streamed() == ()
        assert backend.played() == ()

    async def test_bad_header_closes_connection(self, rig) -> None:
        _, _, backend, port = rig
        ws = await _connect(port, "/audio/out")
        # Send >512 bytes of garbage so the header parser gives up.
        await ws.send(b"NOTAWAV" + b"\x00" * 600)
        # Server should close with 1003.
        try:
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        except websockets.exceptions.ConnectionClosed as exc:
            assert exc.code == 1003
        finally:
            await ws.close()
        assert backend.streamed() == ()


class TestAudioIn:
    async def test_capture_frames_reach_client(self, rig) -> None:
        _, _, backend, port = rig
        ws = await _connect(port, "/audio/in")
        try:
            # Give the server a moment to register the capture callback.
            await asyncio.sleep(0.1)
            backend.emit_capture(b"frame-1")
            backend.emit_capture(b"frame-2")
            f1 = await asyncio.wait_for(ws.recv(), timeout=1.0)
            f2 = await asyncio.wait_for(ws.recv(), timeout=1.0)
            assert (f1, f2) == (b"frame-1", b"frame-2")
        finally:
            await ws.close()

    async def test_second_connect_gets_busy_close(self, rig) -> None:
        _, _, _, port = rig
        ws1 = await _connect(port, "/audio/in")
        try:
            await asyncio.sleep(0.1)
            # Second connection while first has capture — should be rejected.
            ws2 = await _connect(port, "/audio/in")
            # Server closes with 1013 (try again later).
            try:
                await asyncio.wait_for(ws2.recv(), timeout=1.0)
                # If we got here without CloseError, no message came — that's also fine
                # since the server may close before sending anything.
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.code == 1013
            await ws2.close()
        finally:
            await ws1.close()

    async def test_client_disconnect_stops_capture(self, rig) -> None:
        _, audio, backend, port = rig
        ws = await _connect(port, "/audio/in")
        await asyncio.sleep(0.1)
        assert audio.is_capturing is True
        await ws.close()
        # Wait for the server side to notice and clean up.
        for _ in range(20):
            if not audio.is_capturing:
                break
            await asyncio.sleep(0.05)
        assert audio.is_capturing is False


class TestAuth:
    async def test_audio_out_requires_token(self, rig) -> None:
        _, _, _, port = rig
        with pytest.raises(websockets.exceptions.InvalidStatus) as excinfo:
            await _connect(port, "/audio/out", token="wrong")
        assert excinfo.value.response.status_code == 401

    async def test_audio_in_requires_token(self, rig) -> None:
        _, _, _, port = rig
        with pytest.raises(websockets.exceptions.InvalidStatus) as excinfo:
            await _connect(port, "/audio/in", token="wrong")
        assert excinfo.value.response.status_code == 401
