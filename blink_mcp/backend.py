"""Where the MCP tools get their camera from.

Two backends, one interface. Either this process owns the camera's live session
(:class:`SessionBackend`), or a bridge already does and we drive its loopback control plane
(:class:`BridgeBackend`). Blink allows one live session per camera, so this choice is not
cosmetic: running both against the same camera means one of them loses the session.

Errors are raised as :class:`BackendError` so the MCP layer can turn them into a tool error
with a message worth reading. Anything else escaping is a bug, and is left to surface as
such.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from .session import BlinkSession

_LOGGER = logging.getLogger(__name__)

COMMANDS = {
    "stop": "stop",
    "home": "go_home",
    "set_home": "set_home",
    "overview": "pan_overview",
}

BRIDGE_ROUTES = {
    "stop": "/ptz/stop",
    "home": "/ptz/home",
    "set_home": "/ptz/set-home",
    "overview": "/ptz/pan360",
}


class BackendError(RuntimeError):
    """A failure the user can act on (no session, bridge down, mount refused)."""


class Backend(Protocol):
    """What the tools need from whatever is holding the camera."""

    async def start(self) -> None:
        """Prepare the backend (connect, log in, open a session)."""
        ...

    async def aclose(self) -> None:
        """Release everything."""
        ...

    async def status(self) -> dict[str, Any]:
        """Return camera, session and mount state."""
        ...

    async def move(self, pan: int, tilt: int, force: bool = False) -> dict[str, Any]:
        """Aim the camera and return the result block."""
        ...

    async def command(self, name: str) -> dict[str, Any]:
        """Run a payload-free command (stop/home/set_home/overview)."""
        ...

    async def wait_for_position(self, timeout: float) -> bool:
        """Wait until the mount has reported an angle."""
        ...

    def stream_url(self) -> str | None:
        """Where the video can be read from, if anywhere."""
        ...


class SessionBackend:
    """Owns the camera's live session in this process (standalone mode)."""

    def __init__(self, session: BlinkSession) -> None:
        """Wrap a session manager."""
        self.session = session

    async def start(self) -> None:
        """Log in and open a liveview."""
        await self.session.start()

    async def aclose(self) -> None:
        """Close the session."""
        await self.session.stop()

    async def status(self) -> dict[str, Any]:
        """Return session, stream and mount state in the same shape the bridge reports.

        Tools read `position`, `session_attached` and `limits_raw` from the top level, so
        the standalone shape has to match the bridge's rather than nest them.
        """
        mount = self.session.controller.status()
        return {
            **mount,
            "session_stats": self.session.stats.as_dict(),
            **self.session.stream.stats(),
        }

    async def move(self, pan: int, tilt: int, force: bool = False) -> dict[str, Any]:
        """Aim the camera, waiting for the mount to confirm."""
        from .control import NoSessionError  # noqa: PLC0415 - avoids a cycle at import time

        try:
            result = await self.session.controller.move_to(pan, tilt, force=force)
        except NoSessionError as error:
            raise BackendError(str(error)) from None
        return result.as_dict()

    async def command(self, name: str) -> dict[str, Any]:
        """Run a payload-free command on the mount."""
        from .control import NoSessionError  # noqa: PLC0415

        if name not in COMMANDS:
            raise BackendError(f"unknown command {name!r}")
        method = getattr(self.session.controller, COMMANDS[name])
        try:
            result = await method()
        except NoSessionError as error:
            raise BackendError(str(error)) from None
        return result.as_dict()

    async def wait_for_position(self, timeout: float) -> bool:
        """Wait for the mount's first position report."""
        return await self.session.wait_for_position(timeout)

    def stream_url(self) -> str | None:
        """Return the local stream this session publishes, if it is up."""
        return self.session.stream.url if self.session.stream.port else None


class BridgeBackend:
    """Drives a bridge that already holds the session, over its loopback control plane."""

    def __init__(self, control_url: str, stream_url: str | None = None) -> None:
        """Store the bridge endpoints."""
        self.control_url = control_url.rstrip("/")
        self._stream_url = stream_url

    async def start(self) -> None:
        """Confirm the bridge is answering, so failure happens at startup not first use."""
        self._call("/ptz/status")

    async def aclose(self) -> None:
        """Nothing to release: the bridge owns the connection."""
        return None

    def _call(self, endpoint: str, payload: dict | None = None, timeout: float = 40.0) -> dict:
        """Call the control plane synchronously (tools are cheap and short)."""
        url = f"{self.control_url}{endpoint}"
        if not url.startswith("http://"):
            raise BackendError("only http:// control URLs are supported")
        request = urllib.request.Request(  # noqa: S310 - loopback, scheme checked above
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"ok": False, "error": raw}
            raise BackendError(
                f"{endpoint} was refused ({error.code}): {body.get('error', raw)}"
            ) from None
        except urllib.error.URLError as error:
            raise BackendError(
                f"the Blink bridge is not reachable at {self.control_url} ({error.reason}). "
                "Start it first, or run this server without --control-url so it owns the "
                "session itself."
            ) from None
        if not body.get("ok"):
            raise BackendError(body.get("error", "the control plane returned an error"))
        return body

    async def status(self) -> dict[str, Any]:
        """Return the bridge's view of the mount."""
        import asyncio  # noqa: PLC0415

        status = await asyncio.to_thread(self._call, "/ptz/status")["status"]
        if self._stream_url:
            status["stream_url"] = self._stream_url
        return status

    async def move(self, pan: int, tilt: int, force: bool = False) -> dict[str, Any]:
        """Ask the bridge to aim the camera (it enforces the closed loop)."""
        import asyncio  # noqa: PLC0415

        body = await asyncio.to_thread(
            self._call, "/ptz/move", {"pan": pan, "tilt": tilt, "force": force}
        )
        return body["result"]

    async def command(self, name: str) -> dict[str, Any]:
        """Ask the bridge to run a payload-free command."""
        import asyncio  # noqa: PLC0415

        if name not in BRIDGE_ROUTES:
            raise BackendError(f"unknown command {name!r}")
        body = await asyncio.to_thread(self._call, BRIDGE_ROUTES[name], {})
        return body["result"]

    async def wait_for_position(self, timeout: float) -> bool:
        """Poll the bridge status until the mount reports an angle."""
        import asyncio  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            status = await self.status()
            if status.get("position"):
                return True
            await asyncio.sleep(0.5)
        return False

    def stream_url(self) -> str | None:
        """Return the stream URL this backend was configured with, if any."""
        return self._stream_url


def build_backend(
    control_url: str | None = None,
    stream_url: str | None = None,
    session_factory: Callable[[], BlinkSession] | None = None,
) -> Backend:
    """Return a bridge backend when a control URL is given, else a standalone session."""
    if control_url:
        _LOGGER.info("using the bridge at %s", control_url)
        return BridgeBackend(control_url, stream_url)
    if session_factory is None:
        raise BackendError(
            "no session factory supplied: standalone mode needs one to build a Blink session"
        )
    return SessionBackend(session_factory())
