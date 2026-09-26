# Installing blink-camera-mcp (instructions for an AI agent)

Follow these steps in order. The install is one command plus credentials — there is nothing to
build, clone, or compile.

## 1. Check prerequisites

- Python 3.10 or newer. `uvx` (recommended) fetches its own interpreter if none is present.
- `ffmpeg` on `PATH` — needed **only** for the `snapshot` tool.
- A Blink account (email + password). If the account has 2FA enabled, you also need a path to a
  file holding the current code — see step 3.

## 2. Add the server to the MCP client config

```json
{
  "mcpServers": {
    "blink": {
      "command": "uvx",
      "args": ["blink-camera-mcp", "--stream-port", "9000"],
      "env": {
        "BLINK_USERNAME": "<account email>",
        "BLINK_PASSWORD": "<account password>"
      }
    }
  }
}
```

`uvx blink-camera-mcp` installs from PyPI on first launch. If the host manages its own Python
instead, `pip install blink-camera-mcp` and use the `blink-mcp` console script as `command`.

Ask the user for the credential values. Never invent them, and never commit them to a file.

## 3. Accounts with 2FA

Add `"BLINK_2FA_FILE": "/absolute/path/to/code.txt"` to `env` and have the user write the current
6-digit code into that file before the server starts. The code is read from the file on demand.

Do not put the code in the command line, in the config, or in a chat message: codes rotate and
the file route exists so they never have to be stored or repeated.

## 4. Verify the install

Ask the server for `camera_status`. A healthy answer reports the current pan/tilt angle, the
mount's travel limits, and `session_attached: true`. Then call `snapshot` once and confirm an
image comes back. Both tools exist only after a successful handshake, so seeing them at all
proves the process started and logged in.

## 5. Things that will bite you

- **One liveview slot per camera.** The Blink phone app competes for the same camera; if it is
  open, the session will not attach until it lets go. This is the most common setup failure.
- **Control rides the video session.** Pan/tilt commands travel over the same connection as the
  stream, so when `session_attached` is false the aim tools fail. That is a real failure — do not
  retry in a loop.
- **Moves are closed-loop.** `pan_tilt` and `pan_tilt_nudge` wait for the mount to report that it
  stopped and return the angle it reported. A value that arrives *before* the command was sent
  means no report came back; treat it as a timeout, not a success.
- **Observation is user-initiated.** `snapshot` looks into the room the camera is in. Call it when
  the user asks for a look, not speculatively.
- **`--stream-port` is optional.** Without it the server still does everything except publish a
  local video stream for OBS/ffmpeg consumers.
