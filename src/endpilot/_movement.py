"""Flat-ground, walking-only movement prediction and goal control.

This module produces Input frames; the application's existing TickEvent observer
remains the sole writer calling Bot.set_input(). No packets or tasks are owned here.
The goal result is *predicted*, not an authoritative BDS arrival assertion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .events import DimensionEvent, PositionEvent, StateEvent, TickEvent
from .types import BotState, BotStatus, Input, Vec2, Vec3

_ACCEL = 0.098
_FRICTION = 0.546
_GROUND_DELTA_Y = -0.0784
_STOP_SPEED = 0.002


@dataclass(frozen=True, slots=True)
class FlatMotion:
    position: Vec3
    vx: float = 0.0
    vz: float = 0.0


def integrate_flat(state: FlatMotion, move: Vec2, yaw: float) -> tuple[FlatMotion, Input]:
    """Predict one 20 Hz tick on confirmed ordinary, flat, solid ground.

    Input.delta is the *post-friction velocity*, not the position difference.
    The vertical value assumes the grounded collision response each tick.
    """
    angle = math.radians(yaw)
    ux = math.cos(angle) * move.x - math.sin(angle) * move.y
    uz = math.sin(angle) * move.x + math.cos(angle) * move.y
    mx = state.vx + _ACCEL * ux
    mz = state.vz + _ACCEL * uz
    next_state = FlatMotion(
        Vec3(state.position.x + mx, state.position.y, state.position.z + mz),
        _FRICTION * mx,
        _FRICTION * mz,
    )
    frame = Input(
        position=next_state.position,
        delta=Vec3(next_state.vx, _GROUND_DELTA_Y, next_state.vz),
        move=move,
        yaw=yaw,
    )
    return next_state, frame


class MovementController:
    """A single-session, walking-only goal intent for an existing input writer.

    Feed events synchronously from a Runtime subscription. On TickEvent, pass
    a non-None returned frame to Bot.set_input() exactly once, after composing
    other application intents. Do not run two independent input writers.
    Position/teleport/dimension corrections fail closed: their packets contain
    no independently verified post-correction ground velocity for this model.
    A caller may explicitly start a new movement after validating BDS state.
    """

    def __init__(self) -> None:
        self.state = "idle"
        self.failure: str | None = None
        self.frames = 0
        self.corrections = 0
        self._session: int | None = None
        self._dimension: int | None = None
        self._motion: FlatMotion | None = None
        self._goal: Vec3 | None = None
        self._tolerance = 0.1
        self._max_ticks = 200
        self._settled = 0
        self._yaw = 0.0

    @property
    def predicted_position(self) -> Vec3 | None:
        return None if self._motion is None else self._motion.position

    def start(self, status: BotStatus, target: Vec3, *, tolerance: float = 0.1,
              max_ticks: int = 200) -> None:
        if status.state != BotState.ready or not status.alive:
            raise RuntimeError("movement requires a live Ready session")
        if not all(math.isfinite(v) for v in (target.x, target.y, target.z, tolerance)):
            raise ValueError("target and tolerance must be finite")
        if not 0 < tolerance <= 0.5 or type(max_ticks) is not int or max_ticks < 1:
            raise ValueError("invalid arrival tolerance or tick limit")
        if abs(target.y - status.position.y) > 0.15:
            raise ValueError("flat movement requires the same grounded height")
        # A changed session cannot inherit old momentum, even after reconnect.
        if self._session != status.session_id or self.state not in ("moving", "coasting"):
            self._motion = FlatMotion(status.position)
        self._session = status.session_id
        self._dimension = status.dimension
        self._goal = target
        self._tolerance = tolerance
        self._max_ticks = max_ticks
        self._settled = 0
        self.frames = 0
        self.corrections = 0
        self.failure = None
        self.state = "moving"

    def stop(self) -> None:
        """Release goal intent immediately; predict residual friction until rest."""
        self._goal = None
        if self.state == "moving":
            self.state = "coasting"

    def _fail(self, reason: str) -> None:
        self.state = "failed"
        self.failure = reason
        self._goal = None
        self._motion = None

    def on_event(self, status: BotStatus, event: object) -> Input | None:
        if self.state not in ("moving", "coasting"):
            return None
        if status.session_id != self._session:
            self._fail("session changed")
            return None
        if isinstance(event, StateEvent):
            if event.state != BotState.ready:
                self._fail("session no longer Ready")
            return None
        if status.state != BotState.ready or not status.alive:
            self._fail("session not ready or player dead")
            return None
        if status.dimension != self._dimension or isinstance(event, DimensionEvent):
            self._fail("dimension changed")
            return None
        if isinstance(event, PositionEvent):
            if event.correction:
                self.corrections += 1
            self._fail("server correction invalidated prediction" if event.correction
                       else "server position reset or teleport")
            return None
        if not isinstance(event, TickEvent):
            return None

        motion = self._motion
        assert motion is not None
        self.frames += 1
        if self.frames > self._max_ticks:
            self._fail("movement exceeded tick limit")
            return None

        move = Vec2()
        if self._goal is not None:
            dx = self._goal.x - motion.position.x
            dz = self._goal.z - motion.position.z
            distance = math.hypot(dx, dz)
            if distance > 1e-10:
                self._yaw = math.degrees(math.atan2(-dx, dz))
            # The sum of this tick's displacement and future free coasting
            # equals (v + a*u)/(1-f). Solve u for zero final position error.
            ux = ((1 - _FRICTION) * dx - motion.vx) / _ACCEL
            uz = ((1 - _FRICTION) * dz - motion.vz) / _ACCEL
            magnitude = math.hypot(ux, uz)
            if magnitude > 1:
                ux /= magnitude
                uz /= magnitude
            heading = math.radians(self._yaw)
            move = Vec2(
                math.cos(heading) * ux + math.sin(heading) * uz,
                -math.sin(heading) * ux + math.cos(heading) * uz,
            )
        self._motion, frame = integrate_flat(motion, move, self._yaw)
        speed = math.hypot(self._motion.vx, self._motion.vz)
        if self.state == "coasting":
            if speed < _STOP_SPEED:
                self.state = "cancelled"
        elif self._goal is not None:
            error = math.hypot(
                self._goal.x - self._motion.position.x,
                self._goal.z - self._motion.position.z,
            )
            self._settled = self._settled + 1 if error <= self._tolerance and speed < _STOP_SPEED else 0
            if self._settled >= 3:
                self.state = "predicted_arrival"
                self._goal = None
        return frame
