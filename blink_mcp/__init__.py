"""MCP server for Amazon Blink cameras.

Exposes a Blink camera and its pan/tilt mount to any Model Context Protocol host: see
where the camera points, aim it, and capture a frame. Works either standalone (it owns
the camera's live session) or against a running bridge, which is useful when the same
camera is already streaming video somewhere else.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
