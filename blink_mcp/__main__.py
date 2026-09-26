"""Run the Blink MCP server.

By default this process owns the camera's live session. Pass ``--control-url`` to drive a
bridge that already holds it instead (useful when the camera is also streaming to OBS etc,
since Blink allows only one session per camera).

Examples:
    blink-mcp --print-config                  show a host config snippet
    blink-mcp                                 standalone, credentials from the environment
    blink-mcp --stream-port 9000              also expose the video on a fixed port
    blink-mcp --control-url http://127.0.0.1:9100 --stream-url tcp://127.0.0.1:9000

"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import pathlib
import sys
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .backend import BackendError, build_backend
from .control import DEFAULT_COMMAND_TIMEOUT, PanTiltController
from .server import create_server
from .session import DEFAULT_SESSION_SECONDS, BlinkSession
from .stream import StreamServer

_LOGGER = logging.getLogger(__name__)


def load_env_file(path: pathlib.Path) -> None:
    """Load KEY=VALUE lines into the environment, without overwriting what is already set."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="blink-mcp",
        description="MCP server for Amazon Blink cameras (see and aim a camera).",
    )
    parser.add_argument("--version", action="version", version=f"blink-mcp {__version__}")
    parser.add_argument(
        "--control-url",
        help="drive a running bridge instead of owning the live session (e.g. "
        "http://127.0.0.1:9100)",
    )
    parser.add_argument(
        "--stream-url",
        help="where a bridge publishes video (e.g. tcp://127.0.0.1:9000); used for snapshots "
        "in --control-url mode",
    )
    parser.add_argument("--camera", help="camera name to drive (default: the only camera)")
    parser.add_argument(
        "--stream-host", default="127.0.0.1", help="interface for the local video stream"
    )
    parser.add_argument(
        "--stream-port",
        type=int,
        default=0,
        help="port for the local video stream (0 = pick a free one; set 9000 for OBS)",
    )
    parser.add_argument("--state-file", help="where to cache the Blink token")
    parser.add_argument(
        "--session-seconds",
        type=float,
        default=DEFAULT_SESSION_SECONDS,
        help=f"rotate the liveview after this long (default {DEFAULT_SESSION_SECONDS:.0f}; "
        "Blink caps a session at 300)",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=DEFAULT_COMMAND_TIMEOUT,
        help=f"how long to wait for the mount to confirm a move (default "
        f"{DEFAULT_COMMAND_TIMEOUT:.0f}s)",
    )
    parser.add_argument("--env-file", help="path to a KEY=VALUE file to load first")
    parser.add_argument(
        "--log-level", default="WARNING", help="DEBUG, INFO, WARNING, ERROR (default WARNING)"
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print an MCP host configuration snippet and exit",
    )
    return parser


def host_config(args: argparse.Namespace) -> dict[str, Any]:
    """Build a host config snippet matching how this process was invoked."""
    argv = ["-m", "blink_mcp"]
    if args.control_url:
        argv += ["--control-url", args.control_url]
    if args.stream_url:
        argv += ["--stream-url", args.stream_url]
    if args.camera:
        argv += ["--camera", args.camera]
    if args.stream_port:
        argv += ["--stream-port", str(args.stream_port)]
    if args.state_file:
        argv += ["--state-file", args.state_file]
    return {"mcpServers": {"blink": {"command": sys.executable, "args": argv}}}


def make_backend(args: argparse.Namespace):
    """Build the backend implied by the arguments."""

    def session_factory() -> BlinkSession:
        return BlinkSession(
            camera_name=args.camera,
            controller=PanTiltController(command_timeout=args.command_timeout),
            stream=StreamServer(host=args.stream_host, port=args.stream_port),
            state_file=pathlib.Path(args.state_file) if args.state_file else None,
            session_seconds=args.session_seconds,
        )

    return build_backend(
        control_url=args.control_url,
        stream_url=args.stream_url,
        session_factory=session_factory,
    )


def build_server(backend, args: argparse.Namespace) -> MCPServer:
    """Wrap a backend in an MCP server with a lifespan that owns its lifetime."""

    @contextlib.asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:  # noqa: ARG001
        try:
            await backend.start()
        except BackendError as error:
            raise SystemExit(f"blink-mcp: {error}") from None
        except Exception as error:  # noqa: BLE001 - report cleanly, do not serve blindly
            raise SystemExit(f"blink-mcp: could not start the session: {error}") from None
        _LOGGER.info("blink-mcp ready")
        try:
            yield {"backend": backend}
        finally:
            await backend.aclose()

    return create_server(backend, lifespan=lifespan)


def main() -> None:
    """Parse arguments and serve MCP on stdio."""
    args = build_parser().parse_args()
    if args.env_file:
        load_env_file(pathlib.Path(args.env_file))
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the JSON-RPC channel: never log there
    )
    if args.print_config:
        print(json.dumps(host_config(args), indent=2))  # noqa: T201 - CLI output
        return
    backend = make_backend(args)
    build_server(backend, args).run()


if __name__ == "__main__":
    main()
