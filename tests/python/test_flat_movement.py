"""Deterministic walking contracts; real BDS arrival is checked by Testbed."""
from dataclasses import replace
import math

from endpilot._movement import FlatMotion, MovementController, integrate_flat
from endpilot import (
    BotState, BotStatus, DimensionEvent, PositionEvent, StateEvent, TickEvent,
    Vec2, Vec3,
)


def _status(position=Vec3(0.5, 101.62001, 0.5), session=1):
    return BotStatus(state=BotState.ready, session_id=session, position=position)


def _tick(controller, status, index):
    frame = controller.on_event(status, TickEvent(index, 0))
    return frame, replace(status, position=frame.position) if frame else status


def test_flat_integrator_matches_probed_motion_and_turn():
    state = FlatMotion(Vec3(0.5, 101.62001, 0.5))
    next_state, frame = integrate_flat(state, Vec2(0, 1), 0)
    assert math.isclose(frame.position.z, 0.598, abs_tol=1e-8)
    assert math.isclose(frame.delta.z, 0.098 * 0.546, abs_tol=1e-8)
    assert math.isclose(frame.delta.y, -0.0784)
    assert math.isclose(next_state.vz, frame.delta.z)
    turned, frame = integrate_flat(state, Vec2(0, 1), 90)
    assert math.isclose(turned.position.x, 0.402, abs_tol=1e-8)
    assert math.isclose(frame.delta.x, -0.098 * 0.546, abs_tol=1e-8)


def test_multiple_targets_and_small_analog_braking():
    for dx, dz in ((0, 0.05), (0, 8), (4, -3), (-2, 2)):
        status = _status()
        controller = MovementController()
        target = Vec3(status.position.x + dx, status.position.y, status.position.z + dz)
        controller.start(status, target)
        analog = False
        for i in range(200):
            frame, status = _tick(controller, status, i)
            if frame is not None:
                analog |= 0 < math.hypot(frame.move.x, frame.move.y) < 0.999
            if controller.state == "predicted_arrival":
                break
        assert controller.state == "predicted_arrival", (dx, dz, controller.failure)
        assert analog
        assert math.hypot(status.position.x - target.x, status.position.z - target.z) < 0.1


def test_retarget_preserves_momentum_and_stop_releases_move():
    status = _status()
    controller = MovementController()
    controller.start(status, Vec3(0.5, status.position.y, 8.5))
    for i in range(8):
        _, status = _tick(controller, status, i)
    old = controller._motion
    controller.start(status, Vec3(-1.5, status.position.y, 3.5))
    assert controller._motion is old
    controller.stop()
    for i in range(20):
        frame, status = _tick(controller, status, i)
        if frame is not None:
            assert frame.move == Vec2()
        if controller.state == "cancelled":
            break
    assert controller.state == "cancelled"
    assert controller.on_event(status, TickEvent(99, 0)) is None


def test_correction_teleport_dimension_and_session_invalidate():
    for event in (PositionEvent(Vec3(0.5, 101.62001, 0.5), correction=True),
                  PositionEvent(Vec3(3, 101.62001, 2), teleport=True),
                  DimensionEvent(1, Vec3()),
                  StateEvent(BotState.closing)):
        status = _status()
        controller = MovementController()
        controller.start(status, Vec3(0.5, status.position.y, 8.5))
        assert controller.on_event(status, event) is None
        assert controller.state == "failed"
        assert controller.on_event(status, TickEvent(1, 0)) is None
    status = _status()
    controller = MovementController()
    controller.start(status, Vec3(0.5, status.position.y, 8.5))
    assert controller.on_event(_status(session=2), TickEvent(1, 0)) is None
    assert controller.failure == "session changed"


def test_tick_limit_and_invalid_height():
    status = _status()
    controller = MovementController()
    controller.start(status, Vec3(0.5, status.position.y, 8.5), max_ticks=2)
    _, status = _tick(controller, status, 1)
    _, status = _tick(controller, status, 2)
    assert controller.on_event(status, TickEvent(3, 0)) is None
    assert controller.failure == "movement exceeded tick limit"
    try:
        controller.start(status, Vec3(0, status.position.y + 1, 0))
    except ValueError:
        pass
    else:
        raise AssertionError("non-flat target accepted")
