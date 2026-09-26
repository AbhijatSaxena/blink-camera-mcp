"""The MCP tools: see where the camera points, aim it, capture a frame.

Tool descriptions are the only instructions an agent ever receives, so the two behaviours
that matter are stated in them: a move returns a position the hardware confirmed (and
raises if it never did), and `snapshot` looks into the room.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .backend import Backend, BackendError

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = float(os.environ.get("BLINK_SNAPSHOT_TIMEOUT", "25"))
SNAPSHOT_PATH = pathlib.Path(tempfile.gettempdir()) / "blink-mcp-snapshot.png"
ANGLE_MIN = -128
ANGLE_MAX = 127

INSTRUCTIONS = (
    "Control a Blink camera and its pan/tilt mount. Angles are whole degrees. "
    "`pan_tilt` returns a position the hardware reported, and fails rather than guessing. "
    "`snapshot` decodes a frame of whatever the camera is pointed at -- call it only when "
    "the user has asked to see the camera, not on your own initiative. Video also streams "
    "at the `stream_url` reported by `camera_status` if the user wants to watch."
)


async def _resolve(awaitable: Any) -> Any:
    """Await a backend call, turning its actionable failures into tool errors."""
    try:
        return await awaitable
    except BackendError as error:
        raise ToolError(str(error)) from None


async def _move(backend: Backend, pan: int, tilt: int, force: bool = False) -> dict[str, Any]:
    """Move and require the mount to confirm it arrived."""
    result = await _resolve(backend.move(pan, tilt, force=force))
    if not result.get("settled"):
        raise ToolError(
            f"the mount did not confirm reaching pan={pan} tilt={tilt}: "
            f"{result.get('detail')} (last reported position: {result.get('position')})"
        )
    return result


async def _nudge(backend: Backend, pan_delta: int, tilt_delta: int) -> dict[str, Any]:
    """Move relative to the angle the mount currently reports."""
    status = await _resolve(backend.status())
    if not status.get("position"):
        await backend.wait_for_position(10.0)
        status = await _resolve(backend.status())
    position = status.get("position")
    if not position:
        raise ToolError(
            "the mount has not reported a position yet (no live session, or the camera "
            "has no pan/tilt mount attached)"
        )
    target_pan = max(ANGLE_MIN, min(ANGLE_MAX, position["pan"] + pan_delta))
    target_tilt = max(ANGLE_MIN, min(ANGLE_MAX, position["tilt"] + tilt_delta))
    return await _move(backend, target_pan, target_tilt)


async def _snapshot(backend: Backend) -> Image:
    """Decode one frame from whatever stream the backend is publishing."""
    status = await _resolve(backend.status())
    stream_url = backend.stream_url() or status.get("stream_url")
    if not stream_url:
        raise ToolError(
            "no video stream is available to capture from: the session is not up yet, "
            "or this server was started against a bridge without --stream-url"
        )
    if status.get("session_attached") is False:
        raise ToolError("the camera has no live session right now, so there is nothing to capture")
    # ffmpeg must not run on the event loop. In standalone mode the video it is waiting for is
    # published by this same loop, so blocking here blocks the stream itself and times out on
    # data that can never arrive. (Against a bridge the bytes come from another process, which
    # is why this only shows up standalone.)
    return Image(path=await asyncio.to_thread(capture_frame, stream_url))


def find_ffmpeg() -> str:
    """Locate an ffmpeg binary (env override, PATH, then common installs)."""
    override = os.environ.get("BLINK_FFMPEG")
    if override:
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
        pathlib.Path("/usr/local/bin"),
        pathlib.Path("/opt/homebrew/bin"),
    ]
    for base in candidates:
        for pattern in ("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe", "ffmpeg*/bin/ffmpeg"):
            for match in sorted(base.glob(pattern)):
                return str(match)
    raise ToolError("ffmpeg not found: install it, or set BLINK_FFMPEG to the binary")


def capture_frame(stream_url: str, timeout: float = SNAPSHOT_TIMEOUT) -> pathlib.Path:
    """Decode a single frame from an MPEG-TS stream into a PNG."""
    ffmpeg = find_ffmpeg()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpegts",
        "-i",
        stream_url,
        "-frames:v",
        "1",
        "-y",
        str(SNAPSHOT_PATH),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, configured binary
            command, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"timed out after {timeout:.0f}s waiting for a frame from {stream_url}"
        ) from None
    except OSError as error:
        raise ToolError(f"could not run ffmpeg: {error}") from None
    if completed.returncode != 0 or not SNAPSHOT_PATH.exists():
        detail = completed.stderr.decode(errors="replace").strip()[-300:]
        raise ToolError(f"could not capture a frame from {stream_url}: {detail}")
    if SNAPSHOT_PATH.stat().st_size == 0:
        raise ToolError(f"captured an empty frame from {stream_url}")
    return SNAPSHOT_PATH


def create_server(
    backend: Backend,
    lifespan: Callable[[MCPServer], AbstractAsyncContextManager[Any]] | None = None,
) -> MCPServer:
    """Build the MCP server whose tools drive the given backend."""
    server = MCPServer(
        name="blink",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool()
    async def camera_status() -> dict[str, Any]:
        """Report the camera's orientation, travel limits and session state.

        Call this first: `position` is null until the camera has a live session, which tells
        you the other tools will fail. It also reports `stream_url`, where the live video can
        be read (e.g. by OBS or ffmpeg).
        """
        return await _resolve(backend.status())

    @server.tool()
    async def pan_tilt(pan: int, tilt: int, force: bool = False) -> dict[str, Any]:
        """Aim the camera at an absolute angle and wait until it gets there.

        `pan` and `tilt` are whole degrees (signed, roughly -128..127). The returned position
        is one the hardware reported; if the mount never confirms arrival this fails instead
        of pretending success. A camera already at the requested angle is left alone and
        reported as settled with `moved: false` -- that is a success. Set `force` to re-send.
        """
        return await _move(backend, pan, tilt, force=force)

    @server.tool()
    async def pan_tilt_nudge(pan_delta: int = 0, tilt_delta: int = 0) -> dict[str, Any]:
        """Turn the camera relative to where it points now, in degrees.

        Prefer this when you have not read the current angle: it reads the mount's own report
        first and keeps the move small. Deltas are clamped to the mount's angle range.
        """
        return await _nudge(backend, pan_delta, tilt_delta)

    @server.tool()
    async def pan_tilt_stop() -> dict[str, Any]:
        """Stop the mount's motors immediately. Harmless when it is already idle."""
        return await _resolve(backend.command("stop"))

    @server.tool()
    async def pan_tilt_home() -> dict[str, Any]:
        """Send the camera to its saved home position and report where it ends up."""
        return await _resolve(backend.command("home"))

    @server.tool()
    async def pan_tilt_set_home() -> dict[str, Any]:
        """Save the camera's current angle as its home position."""
        return await _resolve(backend.command("set_home"))

    @server.tool()
    async def pan_tilt_overview() -> dict[str, Any]:
        """Start a full 360 degree pan overview. Returns immediately; the sweep takes a while."""
        return await _resolve(backend.command("overview"))

    @server.tool()
    async def snapshot() -> Image:
        """Capture one still frame of whatever the camera is pointed at.

        This looks into the room, so call it only when the user asks to see the camera. The
        frame comes from the stream the camera is already sending, so it does not start a
        second session.
        """
        return await _snapshot(backend)

    return server
