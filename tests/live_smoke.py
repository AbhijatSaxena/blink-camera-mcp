"""Live smoke test: run the packaged server standalone against a real camera.

Not part of the offline suite: it needs Blink credentials (or a cached token), a camera,
ffmpeg, and it moves the hardware. It speaks MCP over stdio to the real entry point, so it
exercises exactly what an agent host would.

The frame check is technical (base64 length and content type); it does not look at the
picture.

Usage:
    python tests/live_smoke.py --state-file C:/poc/blink/blink_state.json --camera G8V1-...
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Host:
    """A minimal MCP host over stdio: enough to initialize and call tools."""

    def __init__(self, command: list[str]) -> None:
        """Spawn the server with pipes."""
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.responses: dict[int, dict] = {}
        self.events: dict[int, threading.Event] = {}
        self.stderr_lines: list[str] = []
        self._next_id = 0
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        """Collect JSON-RPC responses."""
        for line in self.process.stdout:
            if not line.strip().startswith("{"):
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = message.get("id")
            if identifier is None:
                continue
            self.responses[identifier] = message
            self.events.setdefault(identifier, threading.Event()).set()

    def _pump_stderr(self) -> None:
        """Keep stderr drained (the server logs there) so it cannot block."""
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip())

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no response expected)."""
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def request(self, method: str, params: dict | None = None, timeout: float = 90.0) -> dict:
        """Send a request and wait for its response."""
        self._next_id += 1
        identifier = self._next_id
        event = threading.Event()
        self.events[identifier] = event
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        if not event.wait(timeout=timeout):
            raise SystemExit(
                f"no response to {method} in {timeout:.0f}s\nstderr tail:\n"
                + "\n".join(self.stderr_lines[-15:])
            )
        return self.responses[identifier]

    def _write(self, payload: dict) -> None:
        """Write one JSON-RPC message."""
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def initialize(self) -> dict:
        """Perform the MCP handshake."""
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "live-smoke", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def call(self, tool: str, arguments: dict, timeout: float = 90.0) -> dict:
        """Call a tool and return its decoded payload."""
        response = self.request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=timeout
        )
        if "error" in response:
            raise SystemExit(f"{tool} errored: {response['error']}")
        result = response["result"]
        if result.get("isError"):
            raise SystemExit(f"{tool} failed: {result['content'][0].get('text')}")
        block = result["content"][0]
        if block["type"] == "image":
            return {"__image__": True, "bytes": len(block.get("data", ""))}
        return json.loads(block["text"])

    def close(self) -> str:
        """Terminate the server and return its stderr for reporting."""
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - best effort teardown
            self.process.kill()
        return "\n".join(self.stderr_lines[-20:])


def main() -> None:
    """Run the handshake, a nudge out and back, then a snapshot."""
    parser = argparse.ArgumentParser(description="Live smoke test for blink-camera-mcp.")
    parser.add_argument("--state-file", required=True, help="cached Blink token file")
    parser.add_argument("--camera", help="camera name")
    parser.add_argument("--delta", type=int, default=4, help="degrees to nudge (default 4)")
    args = parser.parse_args()

    command = [
        sys.executable,
        "-m",
        "blink_mcp",
        "--state-file",
        args.state_file,
        "--log-level",
        "INFO",
    ]
    if args.camera:
        command += ["--camera", args.camera]
    host = Host(command)
    try:
        print("=== initialize ===")
        handshake = host.initialize()
        info = handshake["result"]["serverInfo"]
        print(f"  server: {info['name']} {info['version']}")

        print("\n=== waiting for a live session and a position report ===")
        status: dict = {}
        deadline = time.time() + 90
        while time.time() < deadline:
            status = host.call("camera_status", {})
            if status.get("position"):
                break
            time.sleep(2)
        print(f"  session  : {status.get('session_attached')}")
        print(f"  position : {status.get('position')}")
        print(f"  limits   : {status.get('limits_raw')}")
        print(f"  stream   : {status.get('stream_url')}")
        print(f"  logins   : {status.get('session_stats', {}).get('login_source')}")
        if not status.get("position"):
            raise SystemExit("no position report: is the mount attached and the camera online?")

        print(f"\n=== pan_tilt_nudge(+{args.delta}) ===")
        moved = host.call("pan_tilt_nudge", {"pan_delta": args.delta, "tilt_delta": 0})
        print(
            f"  {moved['detail']}  settled={moved['settled']} moved={moved['moved']} "
            f"elapsed={moved['elapsed']}s -> {moved['position']}"
        )

        print(f"\n=== pan_tilt_nudge(-{args.delta}) ===")
        back = host.call("pan_tilt_nudge", {"pan_delta": -args.delta, "tilt_delta": 0})
        print(f"  {back['detail']}  settled={back['settled']} -> {back['position']}")

        print("\n=== snapshot ===")
        frame = host.call("snapshot", {})
        print(f"  image payload: {frame.get('bytes')} base64 chars")
        if not frame.get("__image__") or frame.get("bytes", 0) < 1000:
            raise SystemExit("snapshot did not return a usable image")

        if not (moved["settled"] and back["settled"]):
            raise SystemExit("the mount did not confirm a move")
        print(
            "\nOK: standalone server logged in, held a session, moved the camera and "
            "captured a frame."
        )
    finally:
        tail = host.close()
        if tail:
            print(f"\n--- server log tail ---\n{tail}")


if __name__ == "__main__":
    main()
