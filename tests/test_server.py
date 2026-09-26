"""Tests for the MCP tool layer and how it runs.

The backend is faked, so request mapping, closed-loop enforcement and error surfacing are
tested without a camera. One test spawns the real CLI against a stub control plane over
stdio, because "it is registered" is not "it runs".
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from blink_mcp import backend as backend_module
from blink_mcp.backend import BackendError, BridgeBackend
from blink_mcp.server import create_server

EXPECTED_TOOLS = {
    "camera_status",
    "pan_tilt",
    "pan_tilt_nudge",
    "pan_tilt_stop",
    "pan_tilt_home",
    "pan_tilt_set_home",
    "pan_tilt_overview",
    "snapshot",
}

STATUS = {
    "session_attached": True,
    "position": {"pan": 113, "tilt": -75, "moving": False},
    "limits_raw": "06ae77f1",
    "position_reports": 5,
    "commands_sent": 2,
}


def move_result(settled: bool = True, moved: bool = True, pan: int = 119, tilt: int = -75) -> dict:
    """Build the result block a backend returns for a move."""
    return {
        "command": "move",
        "requested": [pan, tilt],
        "settled": settled,
        "moved": moved,
        "elapsed": 0.7,
        "detail": "mount reported idle at the requested angle" if settled else "timed out",
        "position": {"pan": pan, "tilt": tilt, "moving": False},
    }


class FakeBackend:
    """Records what the tools asked for and answers with canned results."""

    def __init__(self, status: dict | None = None, move: dict | None = None) -> None:
        """Start with default answers."""
        self.status_block = dict(status or STATUS)
        self.move_result = dict(move or move_result())
        self.calls: list[tuple] = []
        self.started = False
        self.closed = False
        self.stream = "tcp://127.0.0.1:54321"
        self.position_waits = 0

    async def start(self) -> None:
        """Record that startup ran."""
        self.started = True

    async def aclose(self) -> None:
        """Record that shutdown ran."""
        self.closed = True

    async def status(self) -> dict:
        """Return the canned status."""
        self.calls.append(("status",))
        return self.status_block

    async def move(self, pan: int, tilt: int, force: bool = False) -> dict:
        """Record and return a move result."""
        self.calls.append(("move", pan, tilt, force))
        return self.move_result

    async def command(self, name: str) -> dict:
        """Record a payload-free command."""
        self.calls.append(("command", name))
        return {"command": name, "settled": True, "moved": False, "detail": "sent"}

    async def wait_for_position(self, timeout: float) -> bool:
        """Record that a wait was attempted."""
        self.position_waits += 1
        return bool(self.status_block.get("position"))

    def stream_url(self) -> str | None:
        """Return the configured stream URL."""
        return self.stream


@pytest.fixture()
def fake() -> FakeBackend:
    """Return a fake backend."""
    return FakeBackend()


def payload(result) -> dict:
    """Decode a tool result's text content."""
    return json.loads(result.content[0].text)


async def test_expected_tools_are_registered(fake: FakeBackend) -> None:
    """Every tool is present, so a host cannot silently lose one."""
    server = create_server(fake)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_every_tool_has_a_usable_description(fake: FakeBackend) -> None:
    """Descriptions exist and say enough to be the agent's only instructions."""
    server = create_server(fake)
    for tool in await server.list_tools():
        assert tool.description
        assert len(tool.description) > 40, tool.name


async def test_camera_status_returns_the_status_block(fake: FakeBackend) -> None:
    """Status passes the backend's report through."""
    server = create_server(fake)
    result = await server.call_tool("camera_status", {})
    assert result.is_error is False
    assert payload(result)["position"]["pan"] == 113


async def test_pan_tilt_sends_absolute_angles(fake: FakeBackend) -> None:
    """A move asks the backend for the requested angle."""
    server = create_server(fake)
    result = await server.call_tool("pan_tilt", {"pan": 119, "tilt": -75})
    assert result.is_error is False
    assert fake.calls[-1] == ("move", 119, -75, False)


async def test_pan_tilt_fails_when_the_mount_does_not_confirm(fake: FakeBackend) -> None:
    """An unconfirmed move is an error, not a plausible-looking success."""
    fake.move_result = move_result(settled=False, moved=False)
    server = create_server(fake)
    with pytest.raises(ToolError) as caught:
        await server.call_tool("pan_tilt", {"pan": 119, "tilt": -75})
    assert "did not confirm" in str(caught.value)


async def test_nudge_reads_the_current_angle_first(fake: FakeBackend) -> None:
    """A nudge is relative to what the mount reported, not to a guess."""
    server = create_server(fake)
    await server.call_tool("pan_tilt_nudge", {"pan_delta": 6, "tilt_delta": 0})
    assert fake.calls[0] == ("status",)
    assert fake.calls[-1] == ("move", 119, -75, False)


async def test_nudge_is_clamped_to_the_byte_range(fake: FakeBackend) -> None:
    """A large nudge cannot overflow the signed-byte angle field."""
    fake.status_block["position"] = {"pan": 120, "tilt": 0, "moving": False}
    server = create_server(fake)
    await server.call_tool("pan_tilt_nudge", {"pan_delta": 100, "tilt_delta": -100})
    assert fake.calls[-1] == ("move", 127, -100, False)


async def test_nudge_waits_then_explains_a_missing_position(fake: FakeBackend) -> None:
    """Without a position report the tool waits, then fails with something actionable."""
    fake.status_block["position"] = None
    server = create_server(fake)
    with pytest.raises(ToolError) as caught:
        await server.call_tool("pan_tilt_nudge", {"pan_delta": 5})
    assert fake.position_waits == 1
    assert "has not reported a position" in str(caught.value)


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("pan_tilt_stop", "stop"),
        ("pan_tilt_home", "home"),
        ("pan_tilt_set_home", "set_home"),
        ("pan_tilt_overview", "overview"),
    ],
)
async def test_simple_commands_map_to_backend_commands(
    fake: FakeBackend, tool: str, command: str
) -> None:
    """The payload-free tools map to the right backend command."""
    server = create_server(fake)
    result = await server.call_tool(tool, {})
    assert result.is_error is False
    assert fake.calls[-1] == ("command", command)


async def test_backend_failure_becomes_a_tool_error(fake: FakeBackend) -> None:
    """A bridge that is down produces an instruction, not a stack trace."""

    async def explode() -> dict:
        raise BackendError("the Blink bridge is not reachable. Start it first.")

    fake.status = explode  # type: ignore[method-assign]
    server = create_server(fake)
    with pytest.raises(ToolError) as caught:
        await server.call_tool("camera_status", {})
    assert "Start it first" in str(caught.value)


class TestSnapshot:
    """The frame grab, including its failure modes."""

    async def test_snapshot_returns_an_image(
        self, fake: FakeBackend, tmp_path, monkeypatch
    ) -> None:
        """A successful grab comes back as image content."""
        frame = tmp_path / "frame.png"
        frame.write_bytes(b"\x89PNG\r\n\x1a\n fake frame")
        monkeypatch.setattr("blink_mcp.server.capture_frame", lambda url, timeout=None: frame)
        server = create_server(fake)
        result = await server.call_tool("snapshot", {})
        assert result.is_error is False
        assert result.content[0].type == "image"

    async def test_snapshot_does_not_block_the_event_loop(
        self, fake: FakeBackend, monkeypatch, tmp_path
    ) -> None:
        """Test ffmpeg runs off the loop: in standalone mode the loop publishes the video."""
        seen: dict = {}

        def fake_capture(url: str, timeout: float | None = None) -> pathlib.Path:
            seen["thread"] = threading.current_thread()
            path = tmp_path / "frame.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n fake frame")
            return path

        monkeypatch.setattr("blink_mcp.server.capture_frame", fake_capture)
        server = create_server(fake)
        await server.call_tool("snapshot", {})
        assert seen["thread"] is not threading.main_thread(), "capture_frame ran on the event loop"

    async def test_snapshot_without_a_stream_is_explained(self, fake: FakeBackend) -> None:
        """No stream URL means an explanation, not an attempt."""
        fake.stream = None
        server = create_server(fake)
        with pytest.raises(ToolError) as caught:
            await server.call_tool("snapshot", {})
        assert "no video stream is available" in str(caught.value)

    async def test_snapshot_refuses_without_a_session(self, fake: FakeBackend) -> None:
        """A bridge with no live session is not asked for a frame."""
        fake.status_block["session_attached"] = False
        server = create_server(fake)
        with pytest.raises(ToolError) as caught:
            await server.call_tool("snapshot", {})
        assert "no live session" in str(caught.value)


class TestBridgeBackend:
    """The backend that drives a running bridge."""

    def test_unreachable_bridge_says_so(self) -> None:
        """A dead bridge produces an actionable message."""
        client = BridgeBackend("http://127.0.0.1:1", None)
        with pytest.raises(BackendError) as caught:
            client._call("/ptz/status")
        assert "not reachable" in str(caught.value)

    def test_non_http_control_url_is_refused(self) -> None:
        """Only http loopback control URLs are accepted."""
        client = BridgeBackend("file:///etc/passwd", None)
        with pytest.raises(BackendError):
            client._call("/ptz/status")


class _ControlStub(BaseHTTPRequestHandler):
    """Answers like a bridge control plane, recording the paths it was asked for."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Answer status requests."""
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        body = json.dumps({"ok": True, "status": STATUS}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ARG002 - keep pytest output clean
        """Silence request logging."""
        return None


def test_cli_handshake_over_stdio() -> None:
    """Run the real CLI against a stub control plane and speak MCP to it."""
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _ControlStub)
    stub.paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    port = stub.server_address[1]

    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "blink-mcp-tests", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "blink_mcp", "--control-url", f"http://127.0.0.1:{port}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    lines: list[str] = []
    answered = threading.Event()

    def pump() -> None:
        """Collect responses until the server closes stdout."""
        for line in process.stdout:
            lines.append(line)
            if line.strip().startswith("{"):
                try:
                    if json.loads(line).get("id") == 2:
                        answered.set()
                except json.JSONDecodeError:
                    continue

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        # Write the requests but keep stdin open, the way a real host does: closing it
        # makes the server shut down before it answers everything.
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        assert answered.wait(timeout=30), f"no tools/list response. output so far: {lines}"
    finally:
        process.stdin.close()
        process.terminate()
        reader.join(timeout=5)
        stub.shutdown()

    responses = {
        json.loads(line).get("id"): json.loads(line)
        for line in lines
        if line.strip().startswith("{")
    }
    assert 1 in responses, f"no initialize response. output: {lines}"
    assert "serverInfo" in responses[1]["result"]
    assert responses[1]["result"]["serverInfo"]["name"] == "blink"
    assert 2 in responses, "no tools/list response"
    assert {tool["name"] for tool in responses[2]["result"]["tools"]} == EXPECTED_TOOLS
    # The backend's startup check really did hit the control plane.
    assert "/ptz/status" in stub.paths  # type: ignore[attr-defined]


def test_backend_factory_prefers_the_bridge() -> None:
    """A control URL selects the bridge backend rather than a session."""
    built = backend_module.build_backend(control_url="http://127.0.0.1:9100", stream_url=None)
    assert isinstance(built, BridgeBackend)
