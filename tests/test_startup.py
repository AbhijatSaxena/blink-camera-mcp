"""The server stays usable when the camera session cannot be started.

A host that spawns this server without credentials --- or with a bridge that is not up ---
used to get a process that exited during startup: no tools listed, nothing to act on, and
invisible to any directory that checks a server by starting it and introspecting. These tests
pin the behaviour that replaced it: record the reason, keep serving, retry in the background,
and fail fast per call instead of timing out.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from blink_mcp import __main__ as cli
from blink_mcp.backend import BackendError
from blink_mcp.server import create_server

REASON = "no cached token and no credentials: set BLINK_USERNAME and BLINK_PASSWORD"


class FailingBackend:
    """Starts by failing, like a machine that has no credentials yet."""

    def __init__(self, failures: int = 1, message: str = REASON) -> None:
        """Fail the first ``failures`` start attempts."""
        self.startup_error: str | None = None
        self.failures = failures
        self.message = message
        self.attempts = 0
        self.closed = False

    async def start(self) -> None:
        """Fail until the configured number of attempts has been used up."""
        self.attempts += 1
        if self.attempts <= self.failures:
            raise BackendError(self.message)

    async def aclose(self) -> None:
        """Record shutdown."""
        self.closed = True

    async def status(self) -> dict:
        """Report the unattached state a real backend reports before its session is up."""
        return {"session_attached": False, "position": None, "stream_url": None}

    async def move(self, pan: int, tilt: int, force: bool = False) -> dict:
        """Fail loudly: nothing must reach the camera while the session is down."""
        raise AssertionError(f"move({pan}, {tilt}, {force}) reached the backend")

    async def command(self, name: str) -> dict:
        """Fail loudly: nothing must reach the camera while the session is down."""
        raise AssertionError(f"command({name}) reached the backend")

    async def wait_for_position(self, timeout: float) -> bool:
        """Fail loudly: nothing must wait on the camera while the session is down."""
        raise AssertionError(f"wait_for_position({timeout}) reached the backend")

    def stream_url(self) -> str | None:
        """No stream before a session exists."""
        return None


async def test_a_failed_start_is_recorded_rather_than_raised() -> None:
    """Startup reports the failure to the caller instead of ending the process."""
    backend = FailingBackend()
    assert await cli.start_backend(backend) is False
    assert backend.startup_error == REASON


async def test_a_successful_start_clears_any_earlier_error() -> None:
    """Recovering is reflected in the state the tools read."""
    backend = FailingBackend(failures=0)
    backend.startup_error = "stale reason"
    assert await cli.start_backend(backend) is True
    assert backend.startup_error is None


async def test_a_failed_start_still_serves_the_tool_list() -> None:
    """Introspection is the whole point: a host with no credentials can still list tools."""
    backend = FailingBackend()
    await cli.start_backend(backend)
    tools = await create_server(backend).list_tools()
    assert len(tools) == 8
    assert "camera_status" in {tool.name for tool in tools}


async def test_status_explains_why_there_is_no_session() -> None:
    """The diagnostic tool carries the reason, so an agent knows what to fix."""
    backend = FailingBackend()
    await cli.start_backend(backend)
    result = await create_server(backend).call_tool("camera_status", {})
    assert result.is_error is False
    body = json.loads(result.content[0].text)
    assert body["session_attached"] is False
    assert body["startup_error"] == REASON


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pan_tilt", {"pan": 119, "tilt": -75}),
        ("pan_tilt_nudge", {"pan_delta": 4}),
        ("pan_tilt_stop", {}),
        ("pan_tilt_home", {}),
        ("pan_tilt_set_home", {}),
        ("pan_tilt_overview", {}),
        ("snapshot", {}),
    ],
)
async def test_every_action_fails_fast_with_the_reason(tool: str, args: dict) -> None:
    """Tools that need the session error out immediately and say why, instead of timing out."""
    backend = FailingBackend()
    await cli.start_backend(backend)
    with pytest.raises(ToolError) as caught:
        await create_server(backend).call_tool(tool, args)
    assert "not up" in str(caught.value)
    assert REASON in str(caught.value)


async def test_the_retry_loop_attaches_once_the_cause_is_fixed(monkeypatch) -> None:
    """Fixing the cause does not need a restart: the background retry connects."""
    backend = FailingBackend(failures=1)
    monkeypatch.setattr(cli, "RETRY_INTERVAL", 0.01)
    assert await cli.start_backend(backend) is False
    await asyncio.wait_for(cli.retry_start(backend), timeout=5)
    assert backend.startup_error is None
    assert backend.attempts == 2


async def test_an_unexpected_start_error_is_reported_too() -> None:
    """Anything that escapes startup is still surfaced, labelled as unexpected."""

    class Boom(FailingBackend):
        async def start(self) -> None:
            raise RuntimeError("permission denied reading the state file")

    backend = Boom()
    assert await cli.start_backend(backend) is False
    reason = backend.startup_error or ""
    assert "could not start the session" in reason
    assert "permission denied" in reason
