"""Publish a session's video as a local MPEG-TS stream for consumers.

The camera's stream is a raw transport stream inside the IMMI session, so anything on this
machine can consume it over TCP: OBS as a Media Source, ffmpeg for a single frame, VLC.

Two details make this work in practice:

* MPEG-TS is only self-describing from the PAT (PID 0) onwards. A consumer connecting
  mid-session would otherwise see raw PES with no program info and refuse the stream, so the
  most recent PAT-and-onwards run is cached and replayed to each new consumer.
* The cache is cleared when a new upstream session starts, so a consumer never receives the
  previous session's packets.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

TS_PACKET = 188
TS_SYNC_BYTE = 0x47
PREFIX_LIMIT = 1 << 20


class StreamServer:
    """A local TCP endpoint republishing MPEG-TS to any number of consumers."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Bind nothing yet; call start()."""
        self.host = host
        self.requested_port = port
        self.port: int | None = None
        self.clients: set[asyncio.StreamWriter] = set()
        self.bytes_published = 0
        self._server: asyncio.AbstractServer | None = None
        self._prefix = bytearray()
        self._leftover = bytearray()

    @property
    def url(self) -> str:
        """Consumer URL, e.g. tcp://127.0.0.1:53421."""
        return f"tcp://{self.host}:{self.port}"

    async def start(self) -> int:
        """Start listening and return the bound port."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.requested_port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.info("stream available on %s", self.url)
        return self.port

    async def stop(self) -> None:
        """Stop listening and drop every consumer."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self.clients):
            writer.close()
        self.clients.clear()
        self.port = None

    def new_session(self) -> None:
        """Forget cached packets: a new upstream session must not inherit the old prefix."""
        self._prefix.clear()
        self._leftover.clear()

    def publish(self, payload: bytes) -> None:
        """Hand a video payload to every consumer."""
        self.bytes_published += len(payload)
        self._track(payload)
        for writer in list(self.clients):
            if writer.is_closing():
                self.clients.discard(writer)
                continue
            try:
                writer.write(payload)
            except (ConnectionResetError, RuntimeError):
                self.clients.discard(writer)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Send the cached PAT prefix, then hold the socket open."""
        peer = writer.get_extra_info("peername")
        self.clients.add(writer)
        _LOGGER.info("stream consumer connected: %s", peer)
        try:
            if self._prefix:
                writer.write(bytes(self._prefix))
                await writer.drain()
            while not writer.is_closing():
                if await reader.read(1024) == b"":
                    break
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            _LOGGER.info("stream consumer gone: %s", peer)

    def _track(self, data: bytes) -> None:
        """Re-frame into TS packets and remember the last PAT-onward run."""
        if self._leftover:
            data = bytes(self._leftover) + data
        complete = (len(data) // TS_PACKET) * TS_PACKET
        for offset in range(0, complete, TS_PACKET):
            packet = data[offset : offset + TS_PACKET]
            if packet[0] != TS_SYNC_BYTE:  # lost sync on this packet; skip it
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid == 0:  # PAT - a new, self-contained starting point
                self._prefix.clear()
            self._prefix.extend(packet)
        self._leftover = bytearray(data[complete:])
        if len(self._prefix) > PREFIX_LIMIT:
            del self._prefix[: len(self._prefix) - PREFIX_LIMIT // 2]

    def stats(self) -> dict:
        """Return counters for status reporting."""
        return {
            "stream_url": self.url if self.port else None,
            "stream_consumers": len(self.clients),
            "stream_bytes": self.bytes_published,
        }
