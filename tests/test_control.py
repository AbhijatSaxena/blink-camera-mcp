"""Tests for the pan/tilt control plane.

Position payloads are ones captured from a live session, so the closed-loop logic is
exercised against bytes the hardware actually sent.
"""

from __future__ import annotations

import asyncio

import pytest

from blink_mcp.control import NoSessionError, PanTiltController
from blink_mcp.immi import (
    ACCESSORY_MESSAGE,
    SESSION_MESSAGE,
    AccessoryMessage,
    PanTiltLimits,
    build_move,
    build_stop,
)

# Captured from a live session on a Mini with the pan/tilt mount attached.
IDLE_AT_113 = bytes.fromhex("0164509b71b500")
MOVING_TO_117 = bytes.fromhex("017dd06e75b510")
ARRIVED_AT_119 = bytes.fromhex("017e290877b500")
CAPTURED_LIMITS = bytes.fromhex("06ae77f1")


class FakeTransport:
    """Records everything written to the session."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.chunks: list[bytes] = []

    async def send(self, message: bytes) -> None:
        """Record a write."""
        self.chunks.append(bytes(message))

    @property
    def sent(self) -> bytes:
        """Everything written, concatenated."""
        return b"".join(self.chunks)


@pytest.fixture()
def controller() -> PanTiltController:
    """Return a controller attached to a fake session."""
    instance = PanTiltController(command_timeout=5.0)
    instance.attach(FakeTransport())
    return instance


def report(controller: PanTiltController, payload: bytes) -> None:
    """Feed a position report the way a session would."""
    controller.handle_message(ACCESSORY_MESSAGE, AccessoryMessage.POSITION, payload)


def test_position_is_tracked_from_captured_payload(controller: PanTiltController) -> None:
    """An idle report updates pan, tilt and moving state."""
    report(controller, IDLE_AT_113)
    assert (controller.position.pan, controller.position.tilt) == (113, -75)
    assert controller.position.is_moving is False


def test_moving_report_is_recognised(controller: PanTiltController) -> None:
    """An in-motion report is flagged as moving."""
    report(controller, MOVING_TO_117)
    assert controller.position.is_moving is True


def test_limits_are_tracked(controller: PanTiltController) -> None:
    """Limits are stored for inspection."""
    controller.handle_message(ACCESSORY_MESSAGE, AccessoryMessage.LIMITS, CAPTURED_LIMITS)
    assert isinstance(controller.limits, PanTiltLimits)
    assert controller.limits.raw == CAPTURED_LIMITS


def test_home_position_is_tracked(controller: PanTiltController) -> None:
    """A HOME_POSITION report is recorded separately."""
    controller.handle_message(ACCESSORY_MESSAGE, AccessoryMessage.HOME_POSITION, ARRIVED_AT_119)
    assert controller.status()["home_position"]["pan"] == 119


def test_non_accessory_messages_are_counted_not_parsed(controller: PanTiltController) -> None:
    """Unrelated message types are counted for diagnostics only."""
    controller.handle_message(SESSION_MESSAGE, 1, b"")
    status = controller.status()
    assert status["other_flags"][hex(SESSION_MESSAGE)] == 1
    assert status["accessory_messages"] == 0
    assert status["position"] is None


async def test_move_sends_the_captured_bytes(controller: PanTiltController) -> None:
    """A move produces exactly the frame the protocol requires."""
    task = asyncio.create_task(controller.move_to(119, -75))
    await asyncio.sleep(0.05)
    report(controller, ARRIVED_AT_119)
    await asyncio.wait_for(task, timeout=2)
    assert controller.transport.sent == build_move(119, -75)


async def test_move_waits_for_the_mount_to_stop_moving(controller: PanTiltController) -> None:
    """An in-motion report does not count as settled."""
    task = asyncio.create_task(controller.move_to(119, -75))
    await asyncio.sleep(0.05)
    report(controller, MOVING_TO_117)
    await asyncio.sleep(0.05)
    assert not task.done()
    report(controller, ARRIVED_AT_119)
    result = await asyncio.wait_for(task, timeout=2)
    assert result.settled is True
    assert result.moved is True
    assert (result.position.pan, result.position.tilt) == (119, -75)
    assert "idle at the requested angle" in result.detail


async def test_move_to_current_angle_is_a_no_op(controller: PanTiltController) -> None:
    """A command to where the mount already points does not drive the motors."""
    report(controller, IDLE_AT_113)
    result = await controller.move_to(113, -75)
    assert result.settled is True
    assert result.moved is False
    assert controller.transport.sent == b""
    assert "already at the requested angle" in result.detail


async def test_move_force_overrides_the_no_op_check(controller: PanTiltController) -> None:
    """Force re-sends even when the angle already matches."""
    report(controller, IDLE_AT_113)
    task = asyncio.create_task(controller.move_to(113, -75, force=True))
    await asyncio.sleep(0.05)
    report(controller, IDLE_AT_113)
    await asyncio.wait_for(task, timeout=2)
    assert controller.transport.sent == build_move(113, -75)


async def test_move_times_out_when_the_mount_never_reports() -> None:
    """A silent mount yields an unsettled result and does not claim a position."""
    controller = PanTiltController(command_timeout=0.3)
    controller.attach(FakeTransport())
    report(controller, IDLE_AT_113)
    result = await controller.move_to(119, -75)
    assert result.settled is False
    assert result.moved is False
    assert "no position report after the command" in result.detail


async def test_move_reports_when_the_mount_stops_short() -> None:
    """A mount that settles away from the target is reported honestly."""
    controller = PanTiltController(command_timeout=0.6)
    controller.attach(FakeTransport())
    task = asyncio.create_task(controller.move_to(119, -75))
    await asyncio.sleep(0.05)
    report(controller, IDLE_AT_113)
    result = await asyncio.wait_for(task, timeout=2)
    assert result.settled is False
    assert "not the requested" in result.detail


async def test_stop_sends_the_stop_frame(controller: PanTiltController) -> None:
    """Stop uses the stop command id and no payload."""
    await controller.stop()
    assert controller.transport.sent == build_stop()


async def test_commands_without_a_session_are_refused(controller: PanTiltController) -> None:
    """A detached controller refuses to pretend it sent anything."""
    controller.detach()
    with pytest.raises(NoSessionError):
        await controller.move_to(10, 10)


async def test_result_is_json_serialisable(controller: PanTiltController) -> None:
    """The result shape is usable by the tool layer."""
    report(controller, IDLE_AT_113)
    result = await controller.move_to(113, -75)
    payload = result.as_dict()
    assert payload["command"] == "move"
    assert payload["settled"] is True
    assert payload["position"]["pan"] == 113
