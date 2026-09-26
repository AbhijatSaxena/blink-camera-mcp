# Container image for blink-camera-mcp.
#
# Directories that evaluate a server by starting it and reading its tool list need no
# credentials for that: since 0.1.1 the server lists its tools and reports a missing session
# through `camera_status` instead of exiting during startup. (Before that it refused to start,
# which is why a quality score could never be computed.)
#
# For real use, pass the BLINK_* environment variables and mount any token/2FA files.

FROM python:3.12-slim

# Only the snapshot tool needs ffmpeg; the rest of the server runs without it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "blink-camera-mcp>=0.1,<0.2"

# MCP over stdio, exactly as a host would run it.
ENTRYPOINT ["blink-mcp"]
