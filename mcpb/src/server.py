#!/usr/bin/env python3
"""Start the packaged MCP server.

The bundle deliberately carries no server code of its own: it depends on
blink-camera-mcp from PyPI and this file just hands control to it, so the
extension can never drift from the released package.
"""

from blink_mcp.__main__ import main

if __name__ == "__main__":
    main()
