"""Own the camera's live session: login, liveview, rotation, framing, dispatch.

Blink brokered live video exists only as a short-lived session (Blink caps it at 300 s),
so a usable tool needs to hold one open, refresh it before it expires, and keep consumers
unaware of the churn. This class does that and fans the single connection out to:

* a :class:`~blink_mcp.stream.StreamServer` that republishes video for OBS/ffmpeg, and
* a :class:`~blink_mcp.control.PanTiltController` that receives accessory messages and can
  send commands back up the same socket.

Framing note: the transport uses a 9-byte header, and a payload is read with
``readexactly`` semantics. Reading "up to n bytes" and treating a short read as fatal is a
common way to kill a liveview a few seconds in, because TCP segmentation is normal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

from .control import NoSessionError, PanTiltController
from .immi import HEADER_SIZE, KEEPALIVE, LATENCY_STATS, VIDEO
from .stream import StreamServer

_LOGGER = logging.getLogger(__name__)

TS_SYNC_BYTE = 0x47
DEFAULT_STATE_FILE = pathlib.Path.home() / ".blink-mcp" / "state.json"
DEFAULT_SESSION_SECONDS = 270.0


@dataclass
class SessionStats:
    """Counters describing how the session is doing."""

    camera: str | None = None
    sessions_opened: int = 0
    sessions_failed: int = 0
    headers_read: int = 0
    video_payloads: int = 0
    video_bytes: int = 0
    last_error: str | None = None
    session_opened_at: float | None = None
    login_source: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot."""
        age = (
            None
            if self.session_opened_at is None
            else round(time.monotonic() - self.session_opened_at, 1)
        )
        return {
            "camera": self.camera,
            "sessions_opened": self.sessions_opened,
            "sessions_failed": self.sessions_failed,
            "session_age_s": age,
            "video_payloads": self.video_payloads,
            "video_bytes": self.video_bytes,
            "last_error": self.last_error,
            "login_source": self.login_source,
            **self.extra,
        }


class BlinkSession:
    """A long-lived Blink liveview that survives Blink's session cap."""

    def __init__(
        self,
        camera_name: str | None = None,
        controller: PanTiltController | None = None,
        stream: StreamServer | None = None,
        state_file: pathlib.Path | None = None,
        session_seconds: float = DEFAULT_SESSION_SECONDS,
    ) -> None:
        """Configure the session; nothing connects until start()."""
        self.camera_name = camera_name
        self.controller = controller or PanTiltController()
        self.stream = stream or StreamServer()
        self.state_file = pathlib.Path(
            state_file or os.environ.get("BLINK_STATE_FILE") or DEFAULT_STATE_FILE
        )
        self.session_seconds = session_seconds
        self.stats = SessionStats(camera=camera_name)
        self.blink: Blink | None = None
        self.camera: Any = None
        self._current: Any = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._http: ClientSession | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Log in, start the local stream, and keep a liveview open in the background."""
        if self._running:
            return
        await self.stream.start()
        self.blink = await self._login()
        self.camera = self._pick_camera()
        self.stats.camera = self.camera.name
        self.controller.attach(self)
        self._running = True
        self._tasks = [
            asyncio.create_task(self._session_loop(), name="blink-session"),
        ]
        _LOGGER.info("session manager started for camera %r", self.camera.name)

    async def stop(self) -> None:
        """Stop the liveview, the local stream, and the HTTP session."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
        self._tasks.clear()
        await self._close_current()
        self.controller.detach()
        await self.stream.stop()
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def wait_for_position(self, timeout: float = 30.0) -> bool:
        """Block until the mount has reported a position, or give up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.controller.position is not None:
                return True
            await asyncio.sleep(0.25)
        return self.controller.position is not None

    # ------------------------------------------------------------------ transport

    async def send(self, message: bytes) -> None:
        """Write a prepared IMMI frame to the current session (Transport protocol)."""
        stream = self._current
        if stream is None or stream.target_writer is None:
            raise NoSessionError("no live session right now - nothing to send on")
        stream.target_writer.write(message)
        await stream.target_writer.drain()

    # ------------------------------------------------------------------ internals

    def _load_login_data(self) -> dict:
        """Token cache first, then credentials from the environment."""
        data: dict = {}
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.stats.login_source = "cached token"
            except json.JSONDecodeError:
                data = {}
        username = os.environ.get("BLINK_USERNAME", "").strip()
        password = os.environ.get("BLINK_PASSWORD", "").strip()
        if username and password:
            data["username"] = username
            data["password"] = password
            self.stats.login_source = "credentials from environment"
        elif not data.get("refresh_token"):
            raise RuntimeError(
                "no cached token and no credentials: set BLINK_USERNAME and BLINK_PASSWORD, "
                "or point BLINK_STATE_FILE at a token written by an earlier login"
            )
        return data

    async def _login(self) -> Blink:
        """Authenticate with Blink, handling 2FA without ever echoing the code."""
        self._http = ClientSession()
        blink = Blink(session=self._http)
        blink.auth = Auth(login_data=self._load_login_data(), no_prompt=True, session=self._http)
        try:
            ok = await blink.start()
        except BlinkTwoFARequiredError:
            _LOGGER.info("Blink asked for a 2FA code; waiting for it to appear")
            await blink.send_2fa_code(await asyncio.to_thread(self._read_2fa_code))
            ok = blink.available or await blink.start()
        if not ok:
            raise RuntimeError("Blink login failed - check the credentials, or re-run a login")
        self._persist_token(blink)
        return blink

    def _persist_token(self, blink: Blink) -> None:
        """Cache token material only; a password must never reach disk."""
        try:
            state = dict(blink.auth.login_attributes)
            state.pop("password", None)
            state.pop("username", None)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, indent=4), encoding="utf-8")
        except Exception:  # noqa: BLE001 - caching is an optimisation, not a requirement
            _LOGGER.warning("could not cache the Blink token", exc_info=True)

    def _read_2fa_code(self) -> str:
        """Read a 2FA code from BLINK_2FA_FILE, waiting for it to appear.

        Deliberately file-only: a code that arrives over a chat transcript or an argument
        list is a code that leaked, and this process may be non-interactive.
        """
        path = os.environ.get("BLINK_2FA_FILE", "").strip()
        if not path:
            raise BlinkTwoFARequiredError(
                "Blink requires a 2FA code. Set BLINK_2FA_FILE to a path, and write the "
                "newest code into it - it is read once and deleted."
            )
        code_file = pathlib.Path(path)
        wait = float(os.environ.get("BLINK_2FA_WAIT", "300"))
        deadline = time.time() + wait
        while time.time() < deadline and not code_file.exists():
            time.sleep(2)
        if not code_file.exists():
            raise BlinkTwoFARequiredError(f"no 2FA code appeared in {code_file} in {wait:.0f}s")
        code = code_file.read_text(encoding="utf-8").strip()
        try:
            code_file.unlink()  # never leave a usable code on disk
        except OSError:
            pass
        return code

    def _pick_camera(self) -> Any:
        """Return the camera to drive, by name or the only one on the account."""
        assert self.blink is not None
        cameras = dict(self.blink.cameras)
        if not cameras:
            raise RuntimeError("no cameras found on this Blink account")
        wanted = (self.camera_name or os.environ.get("BLINK_CAMERA_NAME", "")).strip().lower()
        if wanted:
            for name, camera in cameras.items():
                if name.lower() == wanted:
                    return camera
            raise RuntimeError(f"no camera named {wanted!r}; found {list(cameras)}")
        return next(iter(cameras.values()))

    async def _session_loop(self) -> None:
        """Keep a liveview open forever, rotating before Blink's cap."""
        failures = 0
        while self._running:
            stream = None
            try:
                stream = await self.camera.init_livestream()
                await stream.auth()
                self._current = stream
                self.stream.new_session()
                self.controller.attach(self)
                self.stats.sessions_opened += 1
                self.stats.session_opened_at = time.monotonic()
                _LOGGER.info(
                    "liveview open (session #%d, rotating in %.0fs)",
                    self.stats.sessions_opened,
                    self.session_seconds,
                )
                reader = asyncio.create_task(self._read(stream))
                keepalive = asyncio.create_task(self._keepalive(stream))
                done, pending = await asyncio.wait(
                    {reader, keepalive}, timeout=self.session_seconds
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    error = task.exception()
                    if error is not None:
                        raise error
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the loop has to survive anything
                failures += 1
                self.stats.sessions_failed += 1
                self.stats.last_error = f"{type(error).__name__}: {error}"
                _LOGGER.warning("liveview session failed (%d): %s", failures, error)
            finally:
                await self._close_current()
            if self._running:
                await asyncio.sleep(min(2 + failures * 3, 30))

    async def _close_current(self) -> None:
        """Tear the current liveview down and stop claiming it is attached."""
        stream, self._current = self._current, None
        self.controller.detach()
        if stream is not None:
            try:
                result = stream.stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    async def _read(self, stream: Any) -> None:
        """Frame the session correctly and route payloads (video vs accessory messages)."""
        reader = stream.target_reader
        while self._running and not reader.at_eof():
            header = await reader.readexactly(HEADER_SIZE)
            msgtype = header[0]
            identifier = int.from_bytes(header[1:5], byteorder="big")
            length = int.from_bytes(header[5:9], byteorder="big")
            payload = await reader.readexactly(length) if length else b""
            self.stats.headers_read += 1

            if msgtype == VIDEO:
                if payload and payload[0] == TS_SYNC_BYTE:
                    self.stats.video_payloads += 1
                    self.stats.video_bytes += len(payload)
                    self.stream.publish(payload)
            else:
                # Accessory and session messages: the mount's state arrives here, and a
                # reader that dropped these would see an uncontrollable mount.
                self.controller.handle_message(msgtype, identifier, payload)

    async def _keepalive(self, stream: Any) -> None:
        """Send the heartbeat the relay expects (liveness is enforced server-side)."""
        writer = stream.target_writer
        sequence = 0
        latency = (
            bytes([LATENCY_STATS]) + (1000).to_bytes(4, "big") + (24).to_bytes(4, "big") + bytes(24)
        )
        ticks = 0
        while self._running and not writer.is_closing():
            if ticks % 10 == 0:
                sequence += 1
                writer.write(
                    bytes([KEEPALIVE]) + sequence.to_bytes(4, "big") + (0).to_bytes(4, "big")
                )
                await writer.drain()
            writer.write(latency)
            await writer.drain()
            ticks += 1
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ reporting

    def status(self) -> dict[str, Any]:
        """Return a combined view of the session, stream and mount."""
        return {
            **self.stats.as_dict(),
            **self.stream.stats(),
            "mount": self.controller.status(),
        }

    def diagnostics(self) -> str:
        """Return a human-readable one-line summary."""
        stats = self.stats
        return (
            f"camera={stats.camera!r} sessions={stats.sessions_opened} "
            f"video={stats.video_bytes} bytes stream={self.stream.url} "
            f"login={stats.login_source}"
        )
