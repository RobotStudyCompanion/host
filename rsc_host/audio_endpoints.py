"""Binary WebSocket endpoints for audio.

Two handlers, both auth-checked by the server before we're called:

  * :func:`audio_out_handler` — client streams WAV bytes to the server as
    binary frames; the server parses the WAV header from the first bytes,
    then feeds subsequent PCM chunks to
    :meth:`~rsc_host.peripherals.audio.Audio.stream` **live**. Playback begins
    as soon as enough header + PCM has arrived — no full-buffer wait, ~tens
    of ms of latency instead of upload-duration.

    An ack is delivered on the JSON channel via events
    (``audio.stream.started`` / ``audio.stream.done``).

  * :func:`audio_in_handler` — starts a capture session on connect and streams
    PCM as binary frames until either side closes. Guarantees the session is
    stopped on disconnect.

    The socket carries binary PCM only. The capture format is not fixed — the
    device runs at 48 kHz stereo while the DSP chain emits mono at a retunable
    stream rate — so clients read it from the ``audio.capture.started`` event
    on the main JSON channel, or by calling ``audio.capture.config``.

Kept separate from :mod:`rsc_host.server` so the transport layer stays
transport-only. Registered from :mod:`rsc_host.__main__`.
"""
from __future__ import annotations

import asyncio
import logging
import struct

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from rsc_host.errors import PeripheralUnavailableError
from rsc_host.peripherals.audio import Audio, CaptureBusyError

log = logging.getLogger(__name__)

# Cap on how much WAV data one upload can be — protects against a runaway
# client filling memory. 20 MB is >2 minutes of s16le stereo at 44.1 kHz.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class _WavHeaderError(ValueError):
    """Raised when the WAV header can't be parsed."""


def _parse_wav_header(header: bytes) -> tuple[int, int, int, int]:
    """Parse a canonical WAV/RIFF header prefix.

    Returns ``(samplerate, channels, sample_width_bytes, data_offset)`` where
    ``data_offset`` is the byte index at which the PCM samples start.

    Handles the common case: RIFF/WAVE/fmt /data, PCM format code 1.
    Does *not* handle exotic chunks (LIST/JUNK/fact) beyond skipping them.
    """
    if len(header) < 44:
        raise _WavHeaderError(f"header too short: {len(header)} bytes")
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise _WavHeaderError("not a RIFF/WAVE file")

    # Walk chunks until we hit 'fmt ' and 'data'.
    pos = 12
    samplerate = channels = sample_width = 0
    data_offset = -1
    while pos + 8 <= len(header):
        chunk_id = header[pos : pos + 4]
        (chunk_size,) = struct.unpack("<I", header[pos + 4 : pos + 8])
        chunk_body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise _WavHeaderError(f"fmt chunk too small: {chunk_size}")
            (fmt_code, channels, samplerate, _br, _ba, bits) = struct.unpack(
                "<HHIIHH", header[chunk_body : chunk_body + 16]
            )
            if fmt_code != 1:
                raise _WavHeaderError(f"unsupported WAV format code: {fmt_code}")
            sample_width = bits // 8
        elif chunk_id == b"data":
            data_offset = chunk_body
            break
        pos = chunk_body + chunk_size + (chunk_size & 1)  # pad to even

    if data_offset < 0 or samplerate == 0:
        raise _WavHeaderError("missing fmt or data chunk in header prefix")
    return samplerate, channels, sample_width, data_offset


def build_audio_out_handler(audio: Audio):
    """Return a path handler that streams incoming WAV bytes live to the sink."""

    async def handler(ws: ServerConnection) -> None:
        # Accumulate incoming binary until we've enough to parse the WAV header,
        # then hand a live async iterator of PCM chunks to Audio.stream().
        header_buf = bytearray()
        total_received = 0
        pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

        async def _pcm_iter():
            """Async iterator drained by Audio.stream(); yields PCM chunks."""
            while True:
                chunk = await pcm_queue.get()
                if chunk is None:  # sentinel: upstream closed
                    return
                yield chunk

        stream_task: asyncio.Task | None = None
        header_parsed = False

        try:
            async for message in ws:
                if isinstance(message, str):
                    log.warning(
                        "/audio/out: dropped text frame (%d chars)", len(message)
                    )
                    continue
                total_received += len(message)
                if total_received > _MAX_UPLOAD_BYTES:
                    log.warning(
                        "/audio/out: upload exceeded %d bytes; closing",
                        _MAX_UPLOAD_BYTES,
                    )
                    await ws.close(code=1009, reason="payload too large")
                    return

                if not header_parsed:
                    header_buf.extend(message)
                    # Try to parse. WAV headers are almost always <100 bytes;
                    # give ourselves a generous 512-byte window before giving up.
                    try:
                        sr, ch, sw, offset = _parse_wav_header(bytes(header_buf))
                    except _WavHeaderError as exc:
                        if len(header_buf) > 512:
                            log.warning("/audio/out: header parse failed: %s", exc)
                            await ws.close(code=1003, reason="bad WAV header")
                            return
                        # Not enough bytes yet — keep collecting.
                        continue

                    header_parsed = True
                    log.info(
                        "/audio/out: streaming %d Hz, %d ch, %d-byte samples",
                        sr, ch, sw,
                    )
                    # Push whatever PCM already lives past the header offset.
                    pcm_prefix = bytes(header_buf[offset:])
                    header_buf.clear()

                    # Kick off Audio.stream in parallel; it starts pulling from
                    # pcm_queue immediately.
                    stream_task = asyncio.create_task(
                        audio.stream(
                            _pcm_iter(),
                            samplerate=sr,
                            channels=ch,
                            sample_width=sw,
                            preempt=True,
                        ),
                        name="audio-out-stream",
                    )
                    if pcm_prefix:
                        await pcm_queue.put(pcm_prefix)
                else:
                    # Header already parsed; this whole message is PCM.
                    await pcm_queue.put(message)
        except ConnectionClosed:
            pass
        finally:
            # Tell _pcm_iter to end, then wait for playback to drain.
            await pcm_queue.put(None)
            if stream_task is not None:
                try:
                    await stream_task
                except Exception:
                    log.exception("/audio/out: stream task raised")

        if not header_parsed and total_received > 0:
            log.warning(
                "/audio/out: client closed with %d bytes but no complete header",
                total_received,
            )

    return handler


def build_audio_in_handler(audio: Audio):
    """Return a path handler that streams capture frames to the client.

    Every frame is binary PCM; nothing else is sent on this socket. The format
    is not inferable from the stream, so it travels on the main JSON channel
    instead: the ``audio.capture.started`` event carries it, and
    ``audio.capture.config`` returns it on demand.
    """

    async def handler(ws: ServerConnection) -> None:
        try:
            queue = await audio.start_capture()
        except CaptureBusyError:
            await ws.close(code=1013, reason="capture busy")
            return
        except PeripheralUnavailableError as exc:
            log.warning("/audio/in: capture unavailable: %s", exc)
            await ws.close(code=1011, reason="capture unavailable")
            return
        except Exception:
            log.exception("/audio/in: start_capture failed")
            await ws.close(code=1011, reason="capture failed")
            return

        try:
            # One long-lived receive task, not one per frame. The previous
            # version created and cancelled a task on every 20 ms period, which
            # is ~100 task allocations per second per listener for no reason.
            recv_task = asyncio.create_task(ws.recv(), name="capture-recv")
            try:
                while True:
                    frame_task = asyncio.create_task(queue.get(), name="capture-get")
                    done, _ = await asyncio.wait(
                        {frame_task, recv_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if recv_task in done:
                        # Client sent something or closed; either way we're done.
                        frame_task.cancel()
                        try:
                            await frame_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        return

                    frame = frame_task.result()
                    if frame == b"":
                        # Sentinel from stop_capture, or the capture process
                        # ending — end of stream either way.
                        return
                    try:
                        await ws.send(frame)
                    except ConnectionClosed:
                        return
            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            # Whether the client left, the server ended the session, or an
            # exception fired — always tear the capture session down.
            try:
                await audio.stop_capture()
            except Exception:
                log.exception("/audio/in: stop_capture failed on exit")

    return handler
