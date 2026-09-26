# blink-camera-mcp

[![ci](https://github.com/AbhijatSaxena/blink-camera-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AbhijatSaxena/blink-camera-mcp/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/blink-camera-mcp.svg)](https://pypi.org/project/blink-camera-mcp/)
[![python](https://img.shields.io/pypi/pyversions/blink-camera-mcp.svg)](https://pypi.org/project/blink-camera-mcp/)

<!-- mcp-name: io.github.AbhijatSaxena/blink-camera-mcp -->

An [MCP](https://modelcontextprotocol.io) server for **Amazon Blink cameras**, including the
**pan/tilt mount** that Blink's own API gives you no way to move.

Install it with `uvx blink-camera-mcp`. It is listed in the official MCP Registry as
`io.github.AbhijatSaxena/blink-camera-mcp`.

Give any MCP host — Claude Desktop, an IDE agent, your own client — the ability to see where
a camera is pointing, aim it, and look through it.

```
camera_status       where it points, travel limits, session state, stream URL
pan_tilt            aim at an absolute angle, wait until the hardware confirms
pan_tilt_nudge      turn relative to where it points now
pan_tilt_stop       stop the motors
pan_tilt_home       go to the saved home position
pan_tilt_set_home   save the current angle as home
pan_tilt_overview   360° sweep
snapshot            one still frame, returned as image content
```

## Why this exists

Blink cameras are cloud devices. There is no local API, no ONVIF, no UVC — video exists only
as a short-lived session brokered by Blink's cloud, capped at 300 seconds. Two consequences:

1. Anything that wants to use the camera has to hold that session open and refresh it.
2. The pan/tilt mount is not a device you can talk to. It has no endpoint of its own: it is
   driven *inside* the camera's media session, and its position comes back on the same socket.
   A client that reads only the video and skips every other message never sees any of that
   traffic, which is why the mount has looked uncontrollable for years.

This server owns the session (or reuses one), frames it correctly, routes the accessory
messages, and exposes the whole thing as MCP tools with closed-loop semantics.

## Install

```bash
pip install blink-camera-mcp          # or without installing: uvx blink-camera-mcp
```

The distribution is `blink-camera-mcp`; the command it installs is `blink-mcp`.

You need Python 3.10+, a Blink account, and `ffmpeg` on PATH for `snapshot`
(set `BLINK_FFMPEG` if it lives somewhere unusual).

## Configure

Credentials come from the environment, and only the first run needs them:

```bash
export BLINK_USERNAME="you@example.com"
export BLINK_PASSWORD="..."
export BLINK_CAMERA_NAME="Front Door"      # optional; defaults to the only camera
```

The token is cached (token material only — a password is never written to disk), so later
runs need no credentials. Cache location defaults to `~/.blink-mcp/state.json`; override with
`BLINK_STATE_FILE`.

**If Blink asks for a 2FA code**, the server will not prompt you and will not take the code
from anywhere but a file:

```bash
export BLINK_2FA_FILE=/path/to/code.txt
```

Write the newest code into that file; it is read once and deleted. This keeps a live
credential out of chat logs, shell history and argument lists.

## Add it to your MCP host

```bash
blink-mcp --print-config
```

```json
{
  "mcpServers": {
    "blink": {
      "command": "uvx",
      "args": ["blink-camera-mcp", "--stream-port", "9000"]
    }
  }
}
```

Hosts that manage their own Python (Claude Desktop, VS Code, LM Studio, …) run `uvx` as above.
If you installed it into a virtualenv instead, set `command` to that interpreter and `args` to
`["-m", "blink_mcp", …]`.

## Two ways to run it

**Standalone (default)** — the server owns the camera's live session and publishes the video
to a local TCP port (`tcp://127.0.0.1:<port>`, or a fixed one with `--stream-port 9000`). Point
OBS at it as a Media Source if you want the camera as a webcam as well.

**Against a bridge** — if something else already holds the session:

```bash
blink-mcp --control-url http://127.0.0.1:9100 --stream-url tcp://127.0.0.1:9000
```

This matters because **Blink allows one live session per camera**: a viewer and a controller
have to share it or take turns. Running two things that each insist on their own session means
one of them loses.

## What "closed-loop" means here

The mount reports its angle *while it is moving*. So `pan_tilt` does not sleep and hope: it
sends the command and returns only when the hardware reports that its motors stopped at the
requested angle — typically well under a second.

```
pan_tilt_nudge(+4)  -> settled=True moved=True elapsed=0.67s -> pan=117 tilt=-75
pan_tilt_nudge(-4)  -> settled=True moved=True               -> pan=113 tilt=-75
```

Two deliberate behaviours, because a confident wrong answer is worse than a failure:

* if the mount never confirms, the tool **fails** rather than returning the last angle it
  happened to know (a stale position dressed up as success would have an agent announce a move
  that never happened);
* a command to the angle the camera already holds is a no-op reported as `moved: false` —
  success, not failure.

## Privacy and safety

* `snapshot` decodes a frame of whatever the camera is pointed at. The tool description says
  so, and tells the agent to use it only when the user asks. Nothing here captures anything on
  its own.
* The server talks to Blink directly, and the token cache holds no password.
* Any bridge control plane is loopback-only by design: it moves a physical camera.

## How it was built

The accessory channel is undocumented. It was recovered from the official Android app — dex
bytecode and the native library's symbol table — and then verified against hardware:

```
frame        [flag:1][id:4 big-endian][length:4 big-endian][payload]
send         flag 0x14 INLINE_COMMAND, id = commandId
             move=3 [0,0,0,0,<pan>,<tilt>,0]   stop=4  home=5  set_home=6  overview=7
receive      flag 0x15 ACCESSORY_MESSAGE, id = message id
             POSITION=2 / HOME_POSITION=3 [<counter>,<pan>,<tilt>,<status>]
             ROSIE_LIMITS=4   PAN_OVERVIEW_COMPLETE=5   lights/siren = 0/1/6/7
```

`status` is `0x00` idle and `0x10` moving. Angles are single signed bytes.

The same protocol knowledge is being contributed upstream to
[blinkpy](https://github.com/fronzbot/blinkpy) so every Blink integration benefits, not just
this server. `blink_mcp/immi.py` is byte-identical to the module submitted there, and switches
to the upstream copy automatically once it ships.

Standing on the shoulders of [blinkpy](https://github.com/fronzbot/blinkpy), which implements
the Blink cloud API and the IMMI transport.

## Limitations

* Verified on a **Blink Mini with the pan/tilt mount**. Other camera families use a different
  live transport (WebRTC with JSON-RPC commands rather than this binary channel) and are not
  supported yet.
* One live session per camera, as above.
* Blink caps a session at 300 s; the server rotates it around 270 s and consumers never notice.
* `snapshot` needs `ffmpeg`; it decodes one frame from the stream rather than opening a second
  session.

## Tests

```bash
pytest                    # 55 offline tests: protocol bytes, closed-loop logic, tool layer,
                          # a real stdio handshake, and a stub control plane
```

The suite is offline and needs no camera. For real hardware:

```bash
python tests/live_smoke.py --state-file ~/.blink-mcp/state.json
```

which logs in, moves the camera out and back, and captures a frame.

## License

MIT
