"""Measure how long video takes to flow in standalone mode, and whether the stream delivers it.

Numbers only -- no frames are inspected.

Usage:  python tests/diag_stream.py C:/poc/blink/blink_state.json [seconds]
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

from blink_mcp.session import BlinkSession


async def main() -> None:
    """Open a session, consume its stream, and report byte counts over time."""
    state = pathlib.Path(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0

    session = BlinkSession(state_file=state)
    await session.start()
    reported = await session.wait_for_position(30)
    print(f"position reported: {reported}  ({session.controller.position})")
    print(f"stream: {session.stream.url}")

    reader, writer = await asyncio.open_connection("127.0.0.1", session.stream.port)
    received = 0
    first_at: float | None = None
    started = time.monotonic()
    synced = False

    async def consume() -> None:
        nonlocal received, first_at, synced
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=1.0)
            except asyncio.TimeoutError:  # not the builtin: differs on 3.10
                continue
            if not chunk:
                return
            received += len(chunk)
            if first_at is None:
                first_at = time.monotonic() - started
            if 0x47 in chunk[:188]:
                synced = True

    consumer = asyncio.create_task(consume())
    try:
        while time.monotonic() - started < seconds:
            await asyncio.sleep(5)
            stats = session.stats
            elapsed = time.monotonic() - started
            print(
                f"t+{elapsed:5.1f}s  reader: payloads={stats.video_payloads:<5} "
                f"bytes={stats.video_bytes:<9} | consumer: bytes={received:<9} "
                f"first_at={'n/a' if first_at is None else f'{first_at:.1f}s'} "
                f"ts_sync={synced}"
            )
            if received > 400_000:
                break
    finally:
        consumer.cancel()
        writer.close()
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
