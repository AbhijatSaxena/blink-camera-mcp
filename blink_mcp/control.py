"""Closed-loop control of a Blink pan/tilt mount over a live session.

The mount has no endpoint of its own: commands ride the camera's media session and its
position reports come back on the same socket. Two consequences shape this module:

* control is only possible while a session is up, and there is one session per camera, so
  the controller is attached to whichever session is current (a session manager re-attaches
  when the session rotates);
* because the mount reports *during* motion, a command can be closed-loop: send it, then
  wait for a report saying the motors stopped at the requested angle, instead of guessing a
  settle delay.

The transport is deliberately abstract: anything with an async ``send(bytes)`` can carry
commands, and the session feeds incoming messages to :meth:`PanTiltController.handle_message`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .immi import (
    ACCESSORY_MESSAGE,
    AccessoryMessage,
    PanTiltLimits,
    PanTiltPosition,
    build_go_home,
    build_move,
    build_pan_overview,
    build_set_home,
    build_stop,
    parse_accessory_message,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT = 20.0
DEFAULT_TOLERANCE = 2
# A command whose target equals the current angle may produce no report at all, since the
# device has no reason to move. Wait briefly for confirmation, then accept.
NO_MOTION_GRACE = 2.0


class Transport(Protocol):
    """A live session commands can be written to."""

    async def send(self, message: bytes) -> None:
        """Write one prepared IMMI frame to the session."""
        ...


class NoSessionError(RuntimeError):
    """Raised when a command is issued with no live session attached."""


@dataclass
class CommandResult:
    """Outcome of a control command."""

    command: str
    sent: bytes
    requested: tuple[int, int] | None = None
    position: PanTiltPosition | None = None
    settled: bool = False
    moved: bool = False
    elapsed: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "command": self.command,
            "requested": list(self.requested) if self.requested else None,
            "settled": self.settled,
            "moved": self.moved,
            "elapsed": round(self.elapsed, 2),
            "detail": self.detail,
            "position": None
            if self.position is None
            else {
                "pan": self.position.pan,
                "tilt": self.position.tilt,
                "moving": self.position.is_moving,
                "status": self.position.status,
            },
        }


@dataclass
class _State:
    """Mutable tracking state, kept in one place so status() stays cheap."""

    position: PanTiltPosition | None = None
    home_position: PanTiltPosition | None = None
    limits: PanTiltLimits | None = None
    position_reports: int = 0
    accessory_messages: int = 0
    commands_sent: int = 0
    last_report_at: float | None = None
    other_flags: dict[int, int] = field(default_factory=dict)


class PanTiltController:
    """Track a mount's reported state and drive it over the current live session."""

    def __init__(
        self,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        tolerance: int = DEFAULT_TOLERANCE,
    ) -> None:
        """Set up the controller; no session is attached yet."""
        self.command_timeout = command_timeout
        self.tolerance = tolerance
        self.transport: Transport | None = None
        self._state = _State()
        self._settled = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ session

    def attach(self, transport: Transport) -> None:
        """Point the controller at the session commands should go out on."""
        self.transport = transport
        _LOGGER.debug("pan/tilt controller attached to a live session")

    def detach(self) -> None:
        """Forget the session (it ended or rotated away)."""
        self.transport = None

    @property
    def attached(self) -> bool:
        """Whether a session is currently attached."""
        return self.transport is not None

    @property
    def position(self) -> PanTiltPosition | None:
        """The last position the mount reported."""
        return self._state.position

    @property
    def limits(self) -> PanTiltLimits | None:
        """The travel limits the mount reported, if it has."""
        return self._state.limits

    # ------------------------------------------------------------------ incoming

    def handle_message(self, msgtype: int, identifier: int, payload: bytes) -> None:
        """Record state carried by a live session message.

        Called by the session for every non-video message; safe to call from the reader
        task, never blocks.
        """
        if msgtype != ACCESSORY_MESSAGE:
            self._state.other_flags[msgtype] = self._state.other_flags.get(msgtype, 0) + 1
            return

        self._state.accessory_messages += 1
        parsed = parse_accessory_message(identifier, payload)
        if isinstance(parsed, PanTiltPosition):
            self._state.position = parsed
            self._state.position_reports += 1
            self._state.last_report_at = time.monotonic()
            if identifier == AccessoryMessage.HOME_POSITION:
                self._state.home_position = parsed
            if not parsed.is_moving:
                self._settled.set()
            _LOGGER.debug(
                "mount position pan=%s tilt=%s moving=%s",
                parsed.pan,
                parsed.tilt,
                parsed.is_moving,
            )
        elif isinstance(parsed, PanTiltLimits):
            self._state.limits = parsed
            _LOGGER.debug("mount limits %s", parsed.raw.hex())

    # ------------------------------------------------------------------ outgoing

    async def _send(self, command: str, message: bytes) -> None:
        if self.transport is None:
            raise NoSessionError(
                "no live session attached - the camera has to be streaming for the mount "
                "to accept commands"
            )
        await self.transport.send(message)
        self._state.commands_sent += 1
        _LOGGER.debug("sent %s (%d bytes)", command, len(message))

    def _at_target(self, position: PanTiltPosition, pan: int, tilt: int) -> bool:
        return (
            abs(position.pan - pan) <= self.tolerance
            and abs(position.tilt - tilt) <= self.tolerance
        )

    async def _await_settled(
        self, pan: int, tilt: int, timeout: float
    ) -> tuple[PanTiltPosition | None, bool, str]:
        """Wait for an idle report at the target angle."""
        started = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return self._state.position, False, "timed out waiting for the mount to settle"
            try:
                await asyncio.wait_for(self._settled.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # asyncio.TimeoutError, NOT the builtin TimeoutError: the two only became the
                # same object in 3.11. Ruff's UP041 rewrites this to the builtin, which
                # silently lets the timeout escape on 3.10 -- which is why pyproject pins
                # ruff's target version to py310.
                return self._state.position, False, "timed out waiting for the mount to settle"

            position = self._state.position
            if position is None:
                self._settled.clear()
                continue
            if self._at_target(position, pan, tilt):
                return position, True, "mount reported idle at the requested angle"
            # A report arrived but the mount is still moving (or is elsewhere).
            self._settled.clear()

    async def move_to(self, pan: int, tilt: int, force: bool = False) -> CommandResult:
        """Move the mount to an absolute angle and wait for it to settle."""
        async with self._lock:
            message = build_move(pan, tilt)
            started = time.monotonic()
            current = self._state.position

            if not force and current is not None and self._at_target(current, pan, tilt):
                return CommandResult(
                    command="move",
                    sent=message,
                    requested=(pan, tilt),
                    position=current,
                    settled=True,
                    moved=False,
                    detail="mount is already at the requested angle",
                )

            self._settled.clear()
            reports_before = self._state.position_reports
            await self._send("move", message)

            position, settled, detail = await self._await_settled(pan, tilt, self.command_timeout)
            moved = position is not None and self._at_target(position, pan, tilt)
            # Only claim the mount stopped somewhere if it actually reported *after* the
            # command went out. A stale position from before the command says nothing about
            # what the mount did with it, and reporting it as "stopped short" would turn a
            # silent mount into a confident wrong answer.
            reported_since = self._state.position_reports > reports_before
            if not settled and reported_since and position is not None and not position.is_moving:
                detail = (
                    f"mount stopped at pan={position.pan} tilt={position.tilt}, "
                    f"not the requested pan={pan} tilt={tilt}"
                )
            elif not settled and not reported_since:
                detail = (
                    "timed out with no position report after the command "
                    "(the mount may not have received it)"
                )

            return CommandResult(
                command="move",
                sent=message,
                requested=(pan, tilt),
                position=position,
                settled=settled,
                moved=moved,
                elapsed=time.monotonic() - started,
                detail=detail,
            )

    async def _simple(self, command: str, message: bytes, grace: float) -> CommandResult:
        """Send a payload-free command and give the mount a moment to report."""
        async with self._lock:
            started = time.monotonic()
            self._settled.clear()
            await self._send(command, message)
            try:
                await asyncio.wait_for(self._settled.wait(), timeout=grace)
            except asyncio.TimeoutError:  # see the note in _await_settled: not the builtin
                pass
            return CommandResult(
                command=command,
                sent=message,
                position=self._state.position,
                settled=True,
                elapsed=time.monotonic() - started,
                detail=f"sent; {self._state.position_reports} position report(s) so far",
            )

    async def stop(self) -> CommandResult:
        """Stop the mount's motors."""
        return await self._simple("stop", build_stop(), NO_MOTION_GRACE)

    async def go_home(self) -> CommandResult:
        """Send the mount to its saved home position."""
        return await self._simple("go_home", build_go_home(), self.command_timeout)

    async def set_home(self) -> CommandResult:
        """Save the current position as home."""
        return await self._simple("set_home", build_set_home(), NO_MOTION_GRACE)

    async def pan_overview(self) -> CommandResult:
        """Start a 360 degree pan overview."""
        return await self._simple("pan_overview", build_pan_overview(), NO_MOTION_GRACE)

    # ------------------------------------------------------------------ reporting

    def status(self) -> dict[str, Any]:
        """Return the controller's view of the mount, for tooling to consume."""
        state = self._state
        age = (
            None
            if state.last_report_at is None
            else round(time.monotonic() - state.last_report_at, 1)
        )
        return {
            "session_attached": self.attached,
            "position": None
            if state.position is None
            else {
                "pan": state.position.pan,
                "tilt": state.position.tilt,
                "moving": state.position.is_moving,
            },
            "home_position": None
            if state.home_position is None
            else {"pan": state.home_position.pan, "tilt": state.home_position.tilt},
            "limits_raw": None if state.limits is None else state.limits.raw.hex(),
            "position_reports": state.position_reports,
            "accessory_messages": state.accessory_messages,
            "last_report_age_s": age,
            "commands_sent": state.commands_sent,
            "other_flags": {hex(flag): count for flag, count in state.other_flags.items()},
        }
