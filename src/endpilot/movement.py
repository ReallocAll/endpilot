"""Player position observations and a minimal server-authoritative walking input."""

import asyncio
import math
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from time import perf_counter_ns
from typing import Literal

from .client_clock import ClientClock, TICK_PERIOD_NS
from .entities import Entities, EntityState, VehicleLinkChange
from .expression import animation
from .navigation import (
    AllGoal, AnyGoal, BlockFaceGoal, BlockInteractionGoal, Cell, EntityNearGoal, EntityVisibilityGoal, FootGoal, NavigationEvent, NavigationGoal,
    NavigationPlan, NavigationResult, NavigationRules, Point,
    _GoalState, _LADDER_APPROACH, _LADDER_DECK_LENGTH, _LADDER_STRIDE, _LOWER_SLAB_STRIDE, _STANDING_HEAD_HEIGHT,
    _StandingGraph,
    _VisibilityBudgetExhausted, _VisibilityScanBudget, _WATER_DECK_LENGTH, _WATER_STRIDE, _aim_point,
    _block_face_cells, _is_gap_jump, _is_ladder_climb, _is_lower_slab_lane, _is_stone_ledge_drop,
    _is_water_crossing, _ladder_descent_geometry, _require_goal, _resolve_goal, _runout,
)
from .player_state import PlayerState, movement_speed_attribute, swimming_metadata


_HORIZONTAL_STEPS = ((0, 1), (1, 0), (0, -1), (-1, 0))
_GROUND_FRICTION = 0.546
_GROUND_ACCELERATION = 0.16277136 / _GROUND_FRICTION**3
_REST_SPEED = 0.003
_PLAYER_HALF_WIDTH = 0.30
_DROP_PICKUP_RADIUS = 1.2
_DROP_DIRECT_PROBE_RADIUS = NavigationRules().search_radius
_FLIGHT_HORIZONTAL_ACCELERATION = 0.049
_FLIGHT_AIR_DRAG = 0.91
_FLIGHT_IDLE_DAMPING = 0.375
_FLIGHT_VERTICAL_DRAG = 0.6
_FLIGHT_HORIZONTAL_BRAKE = 1 - _FLIGHT_AIR_DRAG * _FLIGHT_IDLE_DAMPING
_FLIGHT_VERTICAL_BRAKE = 1 - _FLIGHT_VERTICAL_DRAG * _FLIGHT_IDLE_DAMPING
_FLIGHT_TAKEOFF_CLEARANCE = 1.5
# A normal grounded jump can still be airborne ten frames after the second press.
_FLIGHT_TAKEOFF_OBSERVATION = 10
_FLIGHT_BRAKING_TICKS = 8
_FLIGHT_HOVER_DRIFT = 0.0005
# Half a second at rest, plus two ticks for delivery and server sampling.
_FLIGHT_HOVER_TICKS = 12
# A rapid release and re-press can toggle Creative flight on the server.
_FLIGHT_JUMP_RELEASE_TICKS = 10
_GAP_FORWARD_FRAMES = 12
_GAP_SETTLE_FRAMES = 6
_GAP_MIN_TICKS = _GAP_FORWARD_FRAMES + 3 + _GAP_SETTLE_FRAMES
_STONE_DROP_FORWARD_FRAMES = 9
_STONE_DROP_SETTLE_FRAMES = 6
_STONE_DROP_MIN_TICKS = _STONE_DROP_FORWARD_FRAMES + 6 + _STONE_DROP_SETTLE_FRAMES
_LOWER_SLAB_SETTLE_FRAMES = 6
# The calibrated walk used 35 moving frames, then three to receive the exit correction and one to stop.
_LOWER_SLAB_MIN_TICKS = 35 + 4 + _LOWER_SLAB_SETTLE_FRAMES
_LOWER_SLAB_NEUTRAL_LIMIT = 24
_LADDER_RELEASE_PROGRESS = 2.0
_LADDER_NEUTRAL_FRAMES = 24
_LADDER_SETTLE_FRAMES = 6
_LADDER_SETTLE_DISTANCE = 0.035
_LADDER_DESCENT_PHASE_FRAMES = {"align": 20, "approach": 59, "hold": 37, "exit": 24, "settle": 12}
_LADDER_DESCENT_HOLD_FRAMES = 24
_WATER_PHASE_TICKS = {
    "enter": 36, "submerge": 15, "prime": 1, "start": 12, "wall": 25,
    "rise": 25, "lip": 8, "brake": 20, "settle": 12,
}
_WATER_MIN_TICKS = sum(_WATER_PHASE_TICKS.values()) + 3
_WATER_MIN_AIR = _WATER_MIN_TICKS + 30
_ACTOR_FLAGS = 0
_ACTOR_FLAGS_EXTENDED = 92

_Landing = tuple[int, int, frozenset[tuple[int, int]]]
_PlayerCorrection = tuple[int, bool, tuple[float, float, float], tuple[float, float, float]]


def _block_column(x: float, z: float) -> tuple[int, int]:
    return math.floor(x), math.floor(z)


def _matches_landing(landing: _Landing, tick: int, pos) -> bool:
    submitted_tick, feet_y, columns = landing
    return (tick >= submitted_tick and abs(float(pos.y) - (feet_y + 1.62)) <= 0.05
            and _block_column(pos.x, pos.z) in columns)


@dataclass(frozen=True, slots=True)
class BlockApproach:
    """Outcome based on observed terrain and the latest predicted stance.

    The frame count records submitted moving input. The feet block and Bedrock face are
    present on arrival. Server position and interaction acceptance are separate
    observations.
    """

    status: Literal["reached", "unobserved", "unreachable", "stopped", "budget_exhausted"]
    frames: int
    stand_position: tuple[int, int, int] | None = None
    face: int | None = None


_FlightStatus = Literal["predicted_arrived", "blocked", "unknown", "budget", "stopped"]


@dataclass(frozen=True, slots=True)
class FlightResult:
    """Local flight prediction at return, with the number of submitted input frames."""

    status: _FlightStatus
    frames: int
    feet_position: tuple[float, float, float] | None


@dataclass(frozen=True, slots=True)
class _FlightInput:
    vector: tuple[float, float] = (0.0, 0.0)
    vertical: int = 0
    yaw: float = 0.0

    def step(self, velocity):
        # BDS creative flight has no gravity. An idle axis damps differently
        # from a held direction. The packet still carries the actual keys.
        strafe, forward = self.vector
        vx, vy, vz = velocity
        if strafe or forward:
            sine, cosine = math.sin(self.yaw), math.cos(self.yaw)
            vx += _FLIGHT_HORIZONTAL_ACCELERATION * (strafe * cosine - forward * sine)
            vz += _FLIGHT_HORIZONTAL_ACCELERATION * (forward * cosine + strafe * sine)
            horizontal_drag = _FLIGHT_AIR_DRAG
        else:
            horizontal_drag = _FLIGHT_AIR_DRAG * _FLIGHT_IDLE_DAMPING
        if self.vertical:
            vy += 0.15 if self.vertical > 0 else -0.22
        elif not (strafe or forward):
            vy *= _FLIGHT_IDLE_DAMPING
        return (vx, vy, vz), (vx * horizontal_drag, vy * _FLIGHT_VERTICAL_DRAG, vz * horizontal_drag)


class _FlightInterrupted(Exception):
    pass


class _LowerSlabDeparture(Exception):
    pass


class _StraightWalkUnsafe(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ForwardInput:
    """Facing in degrees and forward distance in blocks for one flat-ground frame."""

    yaw: float
    distance: float

    def __post_init__(self):
        if not math.isfinite(self.yaw) or not math.isfinite(self.distance) or self.distance < 0:
            raise ValueError("forward input requires a finite yaw and a finite nonnegative distance")


@dataclass(frozen=True, slots=True)
class MovementState:
    """Predicted feet position and velocity in blocks per client tick.

    Ground describes server observations, with an unknown state after an
    ungrounded teleport until the next travel observation. Corrections count
    every received local-player correction. Sprint and sneak describe submitted
    inputs. Glide and crawl reflect local-player metadata, with None until observed.
    """

    feet_position: tuple[float, float, float] | None
    velocity: tuple[float, float, float]
    on_ground: bool | None
    correction_count: int
    moving: bool
    sprint_key_down: bool
    sprinting: bool
    sneaking: bool
    gliding: bool | None = None
    crawling: bool | None = None


@dataclass(slots=True)
class _PoseIntent:
    enabled: bool
    awaiting_opposite: bool
    confirmed: bool = False

    def observe(self, value: bool | None) -> None:
        if not self.confirmed:
            if value is not self.enabled:
                self.awaiting_opposite = False
            elif not self.awaiting_opposite:
                self.confirmed = True


@dataclass(slots=True)
class _BoatInput:
    entity_id: int
    runtime_id: int
    directions: tuple[float, float]
    correction_tick: int
    player_pose_anchor: EntityState | None = None


class _FollowTargetLost(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _StandingNearState(_GoalState):
    clearance: Callable[[Point, Point], bool | None] = field(kw_only=True)

    def check(self, cell, *, eye=None, visibility_standing=None):
        near, matches = _GoalState.check(self, cell, eye=eye, visibility_standing=visibility_standing)
        if near is not True:
            return near, matches
        x, feet_y, z = cell
        feet = (x + 0.5, feet_y, z + 0.5) if eye is None else (eye[0], feet_y, eye[2])
        return self.clearance(feet, self.point), matches


class Movement:
    def __init__(self, packets, player_id, *, input_source: Callable[[], ForwardInput] | None = None,
                 on_input_sent: Callable[[int, int], None] | None = None, clock: ClientClock | None = None,
                 movement_speed: float | None = None, entities: Entities | None = None, _dimensions=None,
                 _gap_jump_allowed: Callable[[], bool] | None = None,
                 _navigation_air_supply: Callable[[], int | None] | None = None):
        self.packets = packets
        self.player_id = int(player_id)
        self._clock = ClientClock() if clock is None else clock
        self._dimensions = _dimensions
        self._input_source = input_source
        self._on_input_sent = on_input_sent
        self._gap_jump_allowed = _gap_jump_allowed
        self._navigation_air_supply = _navigation_air_supply
        self.position: tuple[float, float, float] | None = None
        self._input_position: tuple[float, float, float] | None = None
        self._ground_correction_tick: int | None = None
        self._server_grounding: _PlayerCorrection | None = None
        self._observe_travel_position: Callable[[int | None, bool | None, tuple[float, float, float], float], None] | None = None
        self._observed_ground: bool | None = None
        self._fall_velocity: float | None = None
        self._actor_flag_words: dict[int, int] = {}
        self._pose_intents: dict[int, _PoseIntent] = {}
        self._pose_candidate: tuple[int, _PoseIntent] | None = None
        self._pose_generation = 0
        self._correction_count = 0
        self._last_correction_tick = -1
        self._flight_start_tick: int | None = None
        self._velocity = (0.0, 0.0, 0.0)
        self._manual_vector: tuple[float, float] | None = None
        self._move_target: tuple[float, float, float] | None = None
        self._movement_speed = 0.1 if movement_speed is None else movement_speed
        self._rotation = (0.0, 0.0)  # pitch, yaw in degrees
        self._ray_pose_current = False
        self._observed_feet_ride_epoch: int | None = None
        self._tick = 1
        self._teleport_pending: int | None = None
        self._sneaking = False
        self._sprint_key = False
        self._sprinting = False
        self._sprint_release_pending = False
        self._swim_submitted: tuple[int, bool] | None = None
        self._swimming: bool | None = None
        self._swim_metadata_tick = 0
        self._swim_epoch = 0
        self._water_swim_tick: int | None = None
        self._jump_floor_y: float | None = None
        self._jump_start_tick: int | None = None
        self._jump_surface: Callable[[float, float], float | None] | None = None
        self._pending_landing: _Landing | None = None
        self._landing_candidate: _Landing | None = None
        self._dimension_epoch = 0
        self._jump_velocity = 0.0
        self._sprint_jump_extra = (0.0, 0.0)
        self._jump_was_pressed = False
        self._jump_release_clock_tick: int | None = None
        self._qa_jump_presses: deque[tuple[bool, bool]] = deque()
        self._updated = asyncio.Event()
        self._unsubscribe = None
        self._action_lock = asyncio.Lock()
        self._stop_requested = False
        self._active_done: asyncio.Event | None = None
        self._navigation_pose: asyncio.Task | None = None
        self._navigation_wake: asyncio.Event | None = None
        self._follow_target_id: int | None = None
        self._ticker: asyncio.Task | None = None
        self._closing = False
        self._server_disconnected = False
        self._entities = entities
        self._boat_input: _BoatInput | None = None
        self._unsubscribe_boat = None
        self._awaiting_dismount_pose = False
        self._respawn_pending = False

    def start(self, *, _before_dispatch=None):
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self._clock.start()
        reader = (self.packets.start_receiving() if _before_dispatch is None
                  else self.packets.start_receiving(_before_dispatch=_before_dispatch))
        if reader is not None:
            reader.add_done_callback(self._reader_ended)
        self._ticker = asyncio.create_task(self._run_inputs(reader))

    def _reader_ended(self, reader) -> None:
        self._qa_jump_presses.clear()
        self._clear_pose_observations()
        if reader.cancelled() and self._ticker is not None:
            self._ticker.cancel()
        self._clock.stop()

    @property
    def _changing_dimension(self) -> bool:
        return self._dimensions is not None and self._dimensions.active is not None

    @property
    def _can_send_input(self) -> bool:
        return (self._input_position is not None and not self._closing
                and not self._changing_dimension and not self._respawn_pending)

    def _require_input(self) -> None:
        if self._closing:
            raise ConnectionError("movement is closed")
        if self._changing_dimension:
            raise RuntimeError("player input is unavailable during a dimension transition")
        if self._respawn_pending:
            raise ConnectionError("movement is waiting for the player to respawn")

    def _clear_pose_observations(self, *, keep_actor_flags: bool = False) -> None:
        self._fall_velocity = None
        if not keep_actor_flags:
            self._actor_flag_words.clear()
        self._pose_intents.clear()
        self._pose_candidate = None
        self._pose_generation += 1

    def _finish_jump(self) -> None:
        self._jump_floor_y = None
        self._jump_start_tick = None
        self._jump_surface = None
        self._jump_velocity = 0.0

    def _clear_boat_input(self, change: VehicleLinkChange | None = None) -> None:
        controls, self._boat_input = self._boat_input, None
        if self._unsubscribe_boat is not None:
            self._unsubscribe_boat()
            self._unsubscribe_boat = None
        if controls is None:
            return
        anchor = controls.player_pose_anchor
        boat = self._entities.get(controls.runtime_id)
        # Vanilla can send the local PLAYER correction just before the NONE link.
        if (change is not None and change.reason == "link" and change.after is None
                and anchor is not None and boat is not None and boat.entity_id == controls.entity_id
                and (boat.position, boat.boat_rotation) == (anchor.position, anchor.boat_rotation)):
            return
        self.position = self._input_position = None
        self._updated.clear()
        self._ray_pose_current = False
        self._awaiting_dismount_pose = True
        self._last_correction_tick = max(self._last_correction_tick, controls.correction_tick - 1)
        self._server_grounding = None
        self._ground_correction_tick = None
        self._observed_ground = None
        self._velocity = (0.0, 0.0, 0.0)

    def _observed_driver_boat(self) -> EntityState | None:
        if self._entities is None:
            return None
        link = self._entities.vehicle()
        if link is None or link.role != "RIDING" or link.vehicle_runtime_id is None:
            return None
        state = self._entities.get(link.vehicle_runtime_id)
        if state is not None and state.entity_id == link.vehicle_entity_id and state.boat_rotation is not None:
            return state
        return None

    def _active_boat(self) -> EntityState | None:
        controls = self._boat_input
        if controls is None:
            return None
        boat = self._observed_driver_boat()
        if boat is None or (boat.entity_id, boat.runtime_id) != (controls.entity_id, controls.runtime_id):
            self._clear_boat_input()
            return None
        return boat

    def _require_ground_input(self) -> None:
        if self._entities is not None and self._entities.vehicle() is not None:
            raise RuntimeError("ground movement requires leaving the vehicle")
        if self._awaiting_dismount_pose:
            raise RuntimeError("player position is not synchronized after dismount")

    def _actor_flag(self, flag: int) -> bool | None:
        index = _ACTOR_FLAGS if flag < 64 else _ACTOR_FLAGS_EXTENDED
        word = self._actor_flag_words.get(index)
        return None if word is None else bool(word & (1 << (flag % 64)))

    def _incompatible_pose(self) -> bool:
        b = self.packets.protocol.binding
        return any(self._actor_flag(flag) for flag in (b.ActorFlags.RIDING, b.ActorFlags.SWIMMING, b.ActorFlags.SLEEPING))

    def _gliding_or_crawling(self) -> bool:
        flags = self.packets.protocol.binding.ActorFlags
        return any(self._actor_flag(flag) or (flag in self._pose_intents and self._pose_intents[flag].enabled)
                   for flag in (flags.GLIDING, flags.CRAWLING))

    def _observe_pose_intent(self, flag: int) -> None:
        value = self._actor_flag(flag)
        intent = self._pose_intents.get(flag)
        if intent is not None:
            intent.observe(value)
            if intent.confirmed:
                del self._pose_intents[flag]
        candidate = self._pose_candidate
        if candidate is not None and candidate[0] == flag:
            candidate[1].observe(value)

    def _want_pose_change(self, flag: int, enabled: bool) -> bool:
        intent = self._pose_intents.get(flag)
        current = self._actor_flag(flag) if intent is None else intent.enabled
        return current is not True if enabled else current is True

    async def _send_pose(self, flag, enabled: bool, generation: int) -> int:
        previous = self._pose_intents.get(flag)
        observed = self._actor_flag(flag)
        # On a reversal, an unchanged flag can predate both requests.
        awaiting_opposite = previous is not None and observed is not (not enabled)
        candidate = _PoseIntent(enabled, awaiting_opposite)
        action = ("START_" if enabled else "STOP_") + flag.name
        # Both intents observe packets until the action lock holder learns the send outcome.
        self._pose_candidate = (flag, candidate)
        try:
            tick = await self._send_input(False, move_vector=self._manual_directions(), pose_action=action)
        finally:
            self._pose_candidate = None
        if generation == self._pose_generation and not self._changing_dimension:
            if candidate.confirmed:
                self._pose_intents.pop(flag, None)
            else:
                self._pose_intents[flag] = candidate
        return tick

    def _on_packet(self, raw, *, startup: bool = False):
        b = self.packets.protocol.binding
        ground_tick = None
        own_exit_pose = False
        if raw.packet_id == b.DisconnectPacket.ID:
            # A kick may arrive before the transport itself closes.
            self._server_disconnected = True
            self.close()
            return
        if self._respawn_pending and raw.packet_id in (
            b.MovePlayerPacket.ID, b.CorrectPlayerMovePredictionPacket.ID, b.ServerPlayerPostMovePositionPacket.ID,
        ):
            return
        if raw.packet_id == b.MovePlayerPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.player_id) != self.player_id:
                return
            own_exit_pose = not packet.riding_id and int(packet.reset_position) != 3
            if self._awaiting_dismount_pose and not own_exit_pose:
                return
            self._observed_ground = bool(packet.on_ground)
            if packet.on_ground:
                self._fall_velocity = None
            if int(packet.reset_position) == 2:
                pitch, yaw = float(packet.rot.x), float(packet.rot.y)
                if math.isfinite(pitch) and math.isfinite(yaw):
                    self._rotation = (pitch, yaw)
                self._teleport_pending = self._tick
                self._swim_epoch += 1
                self._swim_submitted = None
                self._water_swim_tick = None
                self._last_correction_tick = self._tick - 1
                # A teleport supplies the new position before travel runs there.
                if not packet.on_ground:
                    self._observed_ground = None
                self._server_grounding = None
                self._fall_velocity = None
                self._velocity = (0.0, 0.0, 0.0)
                pos = packet.pos
                # A rejected move can reassert the last position and facing.
                if self.position != (float(pos.x), float(pos.y), float(pos.z)):
                    self._move_target = None
                self._finish_jump()
                self._pending_landing = None
                self._sprint_jump_extra = (0.0, 0.0)
        elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.prediction_type) == 1:
                tick = int(packet.tick)
                boat = self._active_boat()
                if boat is not None and self._boat_input.correction_tick < tick < self._tick:
                    self._boat_input.correction_tick = tick
                    self._entities._apply_boat_correction(boat, packet)
                return
            if int(packet.prediction_type) != 0:
                return
            self._correction_count += 1
            tick = int(packet.tick)
            if tick <= self._last_correction_tick or tick >= self._tick:
                return
            own_exit_pose = self._boat_input is None or tick >= self._boat_input.correction_tick
            if not own_exit_pose:
                return
            self._last_correction_tick = tick
            on_ground = bool(packet.on_ground)
            self._observed_ground = on_ground
            self._velocity = (float(packet.pos_delta.x), float(packet.pos_delta.y), float(packet.pos_delta.z))
            self._fall_velocity = self._velocity[1] if not on_ground and self._velocity[1] < 0 else None
            corrected = (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z))
            self._server_grounding = (tick, on_ground, corrected, self._velocity)
            if self._observe_travel_position is not None:
                self._observe_travel_position(tick, on_ground, corrected, self._velocity[1])
            if on_ground:
                ground_tick = tick
                if self._jump_start_tick is not None and tick >= self._jump_start_tick:
                    self._finish_jump()
                    self._sprint_jump_extra = (0.0, 0.0)
                if self._pending_landing is not None and _matches_landing(self._pending_landing, tick, packet.pos):
                    self._pending_landing = None
                if self._landing_candidate is not None and _matches_landing(self._landing_candidate, tick, packet.pos):
                    self._landing_candidate = None
        elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
            packet = self.packets.protocol.decode(raw)
        elif raw.packet_id == b.UpdateAttributesPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.runtime_id) == self.player_id:
                speed = movement_speed_attribute(packet)
                if speed is not None:
                    self._movement_speed = speed
            return
        elif raw.packet_id == b.SetActorDataPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.id) == self.player_id and (startup or not self._respawn_pending):
                observed = swimming_metadata(packet, b)
                stale_swimming = False
                if observed is not None:
                    tick = int(packet.tick)
                    stale_swimming = (self._water_swim_tick is not None
                                      and tick < max(self._water_swim_tick, self._swim_metadata_tick))
                    if not stale_swimming:
                        self._swimming = observed
                        self._swim_metadata_tick = tick
                for entry in packet.packed_items.data:
                    index = int(entry.id)
                    if index in (_ACTOR_FLAGS, _ACTOR_FLAGS_EXTENDED) and isinstance(entry.payload, b.DataItemInt64Payload):
                        flags = int(entry.payload.value)
                        if stale_swimming and index == _ACTOR_FLAGS:
                            swim_bit = 1 << int(b.ActorFlags.SWIMMING)
                            flags = (flags & ~swim_bit) | (swim_bit if self._swimming is True else 0)
                        self._actor_flag_words[index] = flags
                        flag = b.ActorFlags.GLIDING if index == _ACTOR_FLAGS else b.ActorFlags.CRAWLING
                        self._observe_pose_intent(flag)
            return
        elif raw.packet_id == b.RespawnPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.runtime_id not in (0, self.player_id) or packet.state not in (
                b.PlayerRespawnState.SEARCHING_FOR_SPAWN, b.PlayerRespawnState.READY_TO_SPAWN,
            ):
                return
            self._respawn_pending = packet.state == b.PlayerRespawnState.SEARCHING_FOR_SPAWN
            self._flight_start_tick = None
            self._clear_swimming()
            self._teleport_pending = None
            self._last_correction_tick = self._tick - 1
            self._server_grounding = None
            self._ground_correction_tick = None
            self._observed_ground = None
            # Before PLAYER_SPAWN, position negotiation and metadata belong
            # to the same newly announced actor. A later spawn replaces it.
            self._clear_pose_observations(keep_actor_flags=startup)
            self._velocity = (0.0, 0.0, 0.0)
            self._clear_manual_input()
            self._finish_jump()
            self._jump_was_pressed = False
            self._pending_landing = None
            self._sprint_jump_extra = (0.0, 0.0)
            if self._respawn_pending:
                self._stop_requested = True
                self._input_position = None
                self._ray_pose_current = False
                self._updated.clear()
                return
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            self._dimension_epoch += 1
            self._flight_start_tick = None
            self._clear_swimming()
            self._ray_pose_current = False
            self._teleport_pending = None
            self._last_correction_tick = self._tick - 1
            self._server_grounding = None
            self._ground_correction_tick = None
            self._observed_ground = None
            self._clear_pose_observations()
            self._velocity = (0.0, 0.0, 0.0)
            self._stop_requested = True
            self._clear_manual_input()
            self._finish_jump()
            self._sprint_jump_extra = (0.0, 0.0)
            self._respawn_pending = self._respawn_pending or packet.respawn
            if packet.loading_screen_id is None or self._respawn_pending:
                # A death return may place the player at a temporary loading coordinate.
                self.position = self._input_position = None
                self._updated.clear()
            else:
                pos = packet.pos
                self.position = self._input_position = (float(pos.x), float(pos.y), float(pos.z))
                self._updated.set()
            return
        else:
            return
        # Keep the correction's pose identity so flight replay can detect a later position update.
        if raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            self.position = corrected
        else:
            self.position = (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z))
        if self._observe_travel_position is not None:
            if raw.packet_id == b.MovePlayerPacket.ID and own_exit_pose and int(packet.reset_position) != 2:
                self._observe_travel_position(None, bool(packet.on_ground), self.position, 0.0)
            elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
                self._observe_travel_position(None, None, self.position, 0.0)
        if raw.packet_id == b.MovePlayerPacket.ID and not own_exit_pose:
            self._observed_feet_ride_epoch = None
        else:
            self._observed_feet_ride_epoch = self._entities._own_unmounted_epoch if self._entities else 0
        self._input_position = self.position
        if not self._ray_pose_current:
            # The first position in the new dimension supersedes the old step.
            self._pending_landing = None
        self._ray_pose_current = True
        self._ground_correction_tick = ground_tick
        self._awaiting_dismount_pose = False
        self._updated.set()
        if self._boat_input is not None:
            boat = self._active_boat()
            if boat is not None:
                self._boat_input.player_pose_anchor = boat if own_exit_pose else None

    async def wait_for_position_sync(self, timeout: float | None = 15) -> tuple[float, float, float]:
        """Wait for a server position, or fail promptly if the stream is lost."""
        if not self._updated.is_set() and hasattr(self.packets, "wait_for_packet"):
            b = self.packets.protocol.binding
            positions = (
                b.MovePlayerPacket.ID, b.CorrectPlayerMovePredictionPacket.ID,
                b.ServerPlayerPostMovePositionPacket.ID, b.RespawnPacket.ID,
            )
            # The packet observer updates _updated before wait_for_packet's
            # matching observer runs. Keep the event wait as well, so a
            # position delivered while registering the waiter is not lost.
            received = asyncio.create_task(self.packets.wait_for_packet(
                positions, timeout=timeout, match=lambda _raw: self._updated.is_set(),
            ))
            updated = asyncio.create_task(self._updated.wait())
            try:
                done, _ = await asyncio.wait(
                    (received, updated), return_when=asyncio.FIRST_COMPLETED
                )
                if received in done:
                    await received  # Propagate timeout or packet-stream failure.
            finally:
                received.cancel()
                updated.cancel()
                await asyncio.gather(received, updated, return_exceptions=True)
        else:
            async with asyncio.timeout(timeout):
                await self._updated.wait()
        if self._closing:
            raise ConnectionError("movement is closed")
        if self._respawn_pending:
            raise ConnectionError("movement is waiting for the player to respawn")
        return self.position

    @property
    def input_position(self) -> tuple[float, float, float] | None:
        """Current input origin, rebased by each server position observation."""
        return self._input_position

    def _actor_interaction_origin(self) -> tuple[float, float, float]:
        self._require_input()
        position = self._input_position
        if not self._ray_pose_current or position is None or not all(map(math.isfinite, position)):
            raise RuntimeError("player position is not synchronized")
        return position

    @property
    def observed_feet_position(self) -> tuple[float, float, float] | None:
        """Latest trusted server position in the current dimension, at feet height."""
        position = self.position
        if (position is None or not self._can_send_input or self.raycast_pose[0] is None
                or not all(map(math.isfinite, position))):
            return None
        ride_epoch = self._entities._own_unmounted_epoch if self._entities else 0
        if ride_epoch is None or self._observed_feet_ride_epoch != ride_epoch:
            return None
        x, y, z = position
        return x, y - 1.62, z

    @property
    def swimming(self) -> bool | None:
        return self._swimming

    @property
    def state(self) -> MovementState:
        feet = None
        if self._input_position is not None:
            x, y, z = self._input_position
            feet = (x, y - 1.62, z)
        moving = (self._active_done is not None or self._move_target is not None
                  or any(self._manual_vector or ())
                  or any(abs(value) >= _REST_SPEED for value in self._velocity))
        flags = self.packets.protocol.binding.ActorFlags
        return MovementState(feet, self._velocity, self._observed_ground, self._correction_count,
                             moving, self._sprint_key, self._sprinting, self._sneaking,
                             self._actor_flag(flags.GLIDING), self._actor_flag(flags.CRAWLING))

    @property
    def manual_feet_position(self) -> tuple[float, float, float] | None:
        """Predicted feet while ordinary input owns movement, including its settled position."""
        return self.state.feet_position if self._manual_vector is not None else None

    @property
    def raycast_pose(self) -> tuple[tuple[float, float, float] | None, tuple[float, float]]:
        """Eye and rotation used to form a client input frame."""
        eye = self._input_position if self._ray_pose_current and self._boat_input is None else None
        return eye, self._rotation

    def _stationary_mining_pose(self):
        eye, rotation = self.raycast_pose
        if eye is None or not all(map(math.isfinite, eye)) or self._jump_floor_y is not None:
            return None
        directions = self._manual_directions()
        if directions is not None and (any(directions) or self._velocity[0] or self._velocity[2]):
            return None
        dx, dz = self._sprint_jump_extra
        if dx or dz:
            # Sprint-jump drag can outlive any change to the float32 input position.
            x, y, z = eye
            vec = self.packets.protocol.binding.Vec3
            if vec(x=x + dx, y=y, z=z + dz) != vec(x=x, y=y, z=z):
                return None
        return eye, rotation

    def _entity_cursor_pose(self) -> tuple[tuple[float, float, float], tuple[float, float]] | None:
        """Observed ordinary player head position and current input facing."""
        feet = self.observed_feet_position
        if feet is None or self._observed_ground is not True:
            return None
        flags = self.packets.protocol.binding.ActorFlags
        if any(self._actor_flag(flag) is not False
               for flag in (flags.RIDING, flags.GLIDING, flags.SWIMMING, flags.SLEEPING, flags.CRAWLING)):
            return None
        sneaking = self._actor_flag(flags.SNEAKING)
        if sneaking != self._sneaking:
            return None
        x, y, z = feet
        return (x, y + (1.17 if sneaking else _STANDING_HEAD_HEIGHT), z), self._rotation

    @property
    def rotation(self) -> tuple[float, float]:
        """Local pitch and yaw in degrees used to form subsequent input."""
        return self._rotation

    def set_rotation(self, pitch: float, yaw: float) -> None:
        pitch, yaw = float(pitch), float(yaw)
        if not math.isfinite(pitch) or not math.isfinite(yaw):
            raise ValueError("pitch and yaw must be finite")
        self._rotation = (pitch, yaw)

    async def look_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        """Set the facing direction for subsequent input; return (pitch, yaw)."""
        if self._input_position is None:
            await self.wait_for_position_sync()
        return self._aim_at(target)

    def _aim_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        x, y, z = self._input_position
        dx, dy, dz = (target[0] - x, target[1] - y, target[2] - z)
        yaw = math.degrees(math.atan2(-dx, dz))
        pitch = math.degrees(-math.atan2(dy, math.hypot(dx, dz)))
        self.set_rotation(pitch, yaw)
        return self._rotation

    async def _send_input(self, forward: bool, item_use_transaction=None, block_actions=(),
                          sneak_changed: bool = False, jump_pressed: bool = False,
                          backward: bool = False, strafe: int = 0,
                          missed_swing: bool = False,
                          flight_switch: bool | None = None,
                          before_send: Callable[[], None] | None = None,
                          supplied: ForwardInput | None = None,
                          move_vector: tuple[float, float] | None = None,
                          jump_surface: Callable[[float, float], float | None] | None = None,
                          swim_change: bool | None = None,
                          pose_action: str | None = None,
                          landing_target: tuple[int, frozenset[tuple[int, int]]] | None = None,
                          jump_edge: bool = True, flight: _FlightInput | None = None,
                          validate_step: Callable[
                              [tuple[float, float, float], tuple[float, float, float]], None
                          ] | None = None):
        """Submit one predicted frame; only a later server observation proves acceptance."""
        self._require_input()
        b = self.packets.protocol.binding
        origin = self._input_position
        tick = self._tick
        generation = self._pose_generation
        correction_tick = self._last_correction_tick
        teleport_pending = self._teleport_pending
        original_jump_tick = self._jump_start_tick
        jump_tick = original_jump_tick
        jump_floor = self._jump_floor_y
        active_surface = self._jump_surface
        jump_velocity = self._jump_velocity
        sprint_jump_extra = self._sprint_jump_extra
        glide_press = pose_action == "START_GLIDING"
        pitch, yaw = self._rotation
        y_rad = math.radians(yaw)
        p_rad = math.radians(pitch)
        boat = self._active_boat()
        if flight is not None:
            if origin is None or boat is not None:
                raise RuntimeError("flight requires a synchronized unmounted player")
            x, y, z = origin
            strafe, direction = flight.vector
            scale = 1.0
            forward, backward = direction > 0.01, direction < -0.01
            sprinting, sprint_changed = False, self._sprinting
            jump_pressed = flight.vertical > 0
            jump_edge = not self._jump_was_pressed
            (dx, dy, dz), next_velocity = flight.step(self._velocity)
            next_pos = (x + dx, y + dy, z + dz)
        elif boat is None:
            if origin is None:
                raise RuntimeError("player position is not synchronized")
            x, y, z = origin
            direction = 1 if forward else -1 if backward else 0
            scale = 0.3 if self._sneaking else 1.0
            if move_vector is None:
                sprinting = self._sprint_key and forward and not self._sneaking
            else:
                strafe, direction = move_vector
                sprinting = ((self._sprint_key or (self._sprinting and not self._sprint_release_pending))
                             and direction >= 0.8 and not self._sneaking)
                forward, backward = scale * direction > 0.01, scale * direction < -0.01
            sprint_changed = sprinting != self._sprinting
            step = (0.09 if supplied is None else supplied.distance) * scale * (1.3 if sprinting else 1.0)
            if move_vector is not None:
                grounded = self._observed_ground is not False and jump_floor is None
                acceleration = (self._movement_speed * _GROUND_ACCELERATION if grounded
                                else 0.026 if sprinting else 0.02)
                step = acceleration * scale
            # Normalize full-strength diagonal input for motion and send both
            # raw key values in the input packet.
            step /= max(1.0, math.hypot(scale * strafe, scale * direction))
            dx = step * (strafe * math.cos(y_rad) - direction * math.sin(y_rad))
            dz = step * (direction * math.cos(y_rad) + strafe * math.sin(y_rad))
            if move_vector is not None:
                dx += self._velocity[0]
                dz += self._velocity[2]
            # A grounded jump starts at 0.42 vertical velocity, then applies
            # gravity of 0.08 and vertical drag of 0.98.
            if jump_pressed and jump_floor is None:
                jump_floor = y
                jump_tick = tick
                active_surface = jump_surface
                jump_velocity = 0.42
                if sprinting:
                    # Sprint-jump takeoff adds a forward impulse of 0.2.
                    jump_dx, jump_dz = -math.sin(y_rad) * 0.2, math.cos(y_rad) * 0.2
                    if move_vector is None:
                        sprint_jump_extra = (jump_dx, jump_dz)
                    else:
                        dx += jump_dx
                        dz += jump_dz
            dx += sprint_jump_extra[0]
            dz += sprint_jump_extra[1]
            next_y = y
            finish_jump = glide_press
            if glide_press:
                assert self._fall_velocity is not None
                next_y += self._fall_velocity
            elif jump_floor is not None:
                next_y = max(jump_floor, y + jump_velocity)
                surface_y = active_surface(x + dx, z + dz) if jump_velocity <= 0 and active_surface is not None else None
                if surface_y is not None and y >= surface_y >= next_y:
                    next_y = surface_y
                    finish_jump = True
                elif next_y <= jump_floor:
                    finish_jump = True
                else:
                    jump_velocity = (jump_velocity - 0.08) * 0.98
            if finish_jump:
                jump_tick = jump_floor = active_surface = None
                jump_velocity = 0.0
            dy = next_y - y
            # The launch frame uses ground friction; airborne frames use air friction.
            friction = 0.546 if jump_floor is None or y == jump_floor else 0.91
            sprint_jump_extra = tuple(v * friction for v in sprint_jump_extra)
            next_pos = (x + dx, next_y, z + dz)
            if move_vector is None:
                next_velocity = (dx, dy, dz)
            else:
                drag = _GROUND_FRICTION if grounded else 0.91
                vx, vz = dx * drag, dz * drag
                next_velocity = (0.0 if abs(vx) < _REST_SPEED else vx, dy,
                                 0.0 if abs(vz) < _REST_SPEED else vz)
        else:
            if forward or backward or strafe or jump_pressed or supplied is not None or pose_action is not None:
                raise RuntimeError("boat input owns movement")
            strafe, direction = self._boat_input.directions
            scale = 1.0
            forward = direction > 0
            sprinting, sprint_changed = False, self._sprinting
            # BDS simulates the paddles from movement input. Repeat the latest
            # authoritative boat pose and velocity independently of the camera.
            next_pos = boat.position
            dx, dy, dz = boat.boat_velocity
        movement = b.Vec2(x=scale * strafe, y=scale * direction)
        flags = ([b.PlayerAuthInputPacket.InputData.UP] if forward else
                 [b.PlayerAuthInputPacket.InputData.DOWN] if backward else [])
        side_threshold = 0 if move_vector is None and flight is None else 0.01
        if scale * strafe > side_threshold:
            flags.append(b.PlayerAuthInputPacket.InputData.LEFT)
        elif scale * strafe < -side_threshold:
            flags.append(b.PlayerAuthInputPacket.InputData.RIGHT)
        if self._sneaking:
            flags.extend((
                b.PlayerAuthInputPacket.InputData.SNEAKING,
                b.PlayerAuthInputPacket.InputData.SNEAK_DOWN,
                b.PlayerAuthInputPacket.InputData.SNEAK_CURRENT_RAW,
                b.PlayerAuthInputPacket.InputData.WANT_DOWN,
            ))
        if sprinting:
            flags.append(b.PlayerAuthInputPacket.InputData.SPRINTING)
        if self._sprint_key and boat is None:
            flags.append(b.PlayerAuthInputPacket.InputData.SPRINT_DOWN)
        if sprint_changed:
            flags.append(
                b.PlayerAuthInputPacket.InputData.START_SPRINTING
                if sprinting else b.PlayerAuthInputPacket.InputData.STOP_SPRINTING
            )
        if swim_change is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.START_SWIMMING if swim_change else
                         b.PlayerAuthInputPacket.InputData.STOP_SWIMMING)
        jump_down = jump_pressed or glide_press
        if jump_down:
            flags.extend((
                b.PlayerAuthInputPacket.InputData.JUMPING,
                b.PlayerAuthInputPacket.InputData.JUMP_DOWN,
                b.PlayerAuthInputPacket.InputData.JUMP_CURRENT_RAW,
                b.PlayerAuthInputPacket.InputData.WANT_UP,
            ))
            if jump_edge:
                flags.append(b.PlayerAuthInputPacket.InputData.JUMP_PRESSED_RAW)
            if flight is None and jump_pressed and dy > 0 and jump_floor is not None and y == jump_floor:
                flags.append(b.PlayerAuthInputPacket.InputData.START_JUMPING)
        elif self._jump_was_pressed:
            flags.append(b.PlayerAuthInputPacket.InputData.JUMP_RELEASED_RAW)
        if pose_action is not None:
            flags.append(getattr(b.PlayerAuthInputPacket.InputData, pose_action))
        if sneak_changed:
            flags.extend((
                (b.PlayerAuthInputPacket.InputData.START_SNEAKING,
                 b.PlayerAuthInputPacket.InputData.SNEAK_PRESSED_RAW)
                if self._sneaking else
                (b.PlayerAuthInputPacket.InputData.STOP_SNEAKING,
                 b.PlayerAuthInputPacket.InputData.SNEAK_RELEASED_RAW)
            ))
        if item_use_transaction is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.PERFORM_ITEM_INTERACTION)
        if block_actions:
            flags.append(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS)
        if missed_swing:
            flags.append(b.PlayerAuthInputPacket.InputData.MISSED_SWING)
        if flight_switch is not None:
            input_data = b.PlayerAuthInputPacket.InputData
            flags.append(input_data.START_FLYING if flight_switch else input_data.STOP_FLYING)
        if flight is not None and flight.vertical:
            input_data = b.PlayerAuthInputPacket.InputData
            flags.append(input_data.ASCEND if flight.vertical > 0 else input_data.DESCEND)
        if teleport_pending is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT)
        if supplied is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.VERTICAL_COLLISION)
        if boat is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.IS_IN_CLIENT_PREDICTED_VEHICLE)
        delta_x, delta_y, delta_z = next_velocity if flight is not None else (dx, dy, dz)
        packet = b.PlayerAuthInputPacket(
            rot=b.Vec2(x=pitch, y=yaw),
            pos=b.Vec3(x=next_pos[0], y=next_pos[1], z=next_pos[2]),
            move=movement,
            y_head_rot=yaw,
            input_data=flags,
            input_mode=b.InputMode.MOUSE,
            play_mode=b.ClientPlayMode.NORMAL,
            new_interaction_model=b.NewInteractionModel.CROSSHAIR,
            interact_rotation=b.Vec2(x=pitch, y=yaw),
            client_tick=tick,
            pos_delta=b.Vec3(x=delta_x, y=delta_y, z=delta_z),
            item_use_transaction=item_use_transaction,
            item_stack_request=None,
            player_block_actions=[
                b.PlayerBlockActionData(
                    player_action_type=getattr(b.PlayerActionType, action),
                    pos=b.BlockPos(x=pos[0], y=pos[1], z=pos[2]),
                    facing=face,
                )
                for action, pos, face in block_actions
            ] or None,
            vehicle_rot=None if boat is None else b.Vec2(x=boat.boat_rotation[0], y=boat.boat_rotation[1]),
            client_predicted_vehicle=None if boat is None else boat.entity_id,
            analog_move_vector=movement,
            camera_orientation=b.Vec3(
                x=-math.sin(y_rad) * math.cos(p_rad),
                y=-math.sin(p_rad),
                z=math.cos(y_rad) * math.cos(p_rad),
            ),
            raw_move_vector=movement,
        )
        # No packet can be dispatched between this hook and entering send().
        if before_send is not None:
            before_send()
        if validate_step is not None:
            validate_step(origin, next_pos)
        # Reserve the tick so that a correction for this frame can be observed during send().
        self._tick += 1
        # An early correction can settle the candidate while the send is pending.
        self._landing_candidate = (tick, *landing_target) if landing_target is not None else None
        try:
            await self.packets.send(packet)
            landing = self._landing_candidate
        finally:
            self._landing_candidate = None
        if generation != self._pose_generation or self._changing_dimension:
            return tick
        if boat is None:
            if self._input_position is origin:
                self._input_position = next_pos
            if self._last_correction_tick == correction_tick:
                self._velocity = next_velocity
        self._sprinting = sprinting
        self._sprint_release_pending = False
        if self._jump_was_pressed and not jump_down:
            self._jump_release_clock_tick = self._clock.tick
        self._jump_was_pressed = jump_down
        if self._teleport_pending != teleport_pending:
            return tick
        self._teleport_pending = None
        if landing is not None:
            self._pending_landing = landing
        if boat is not None or flight is not None or self._jump_start_tick != original_jump_tick:
            return tick
        if jump_tick == tick and self._server_grounding is not None and self._server_grounding[:2] == (tick, True):
            # A correction can settle a new jump before it is committed.
            self._sprint_jump_extra = (0.0, 0.0)
        else:
            self._jump_start_tick = jump_tick
            self._jump_floor_y = jump_floor
            self._jump_surface = active_surface
            self._jump_velocity = jump_velocity
            self._sprint_jump_extra = sprint_jump_extra
        return tick

    async def _release_input(self, move_vector=None):
        if self._can_send_input:
            await self._send_input(False, move_vector=move_vector)
            await asyncio.sleep(0.05)
            if self._can_send_input:
                await self._send_input(False, move_vector=move_vector)

    async def _run_inputs(self, reader):
        last_tick = self._clock.tick
        eligible_ns = 0
        while True:
            try:
                last_tick = await self._clock.wait_until_tick(last_tick + 1)
            except (Exception, asyncio.CancelledError):
                self._qa_jump_presses.clear()
                raise
            deadline_ns = self._clock.deadline_ns
            if (self._changing_dimension or self._respawn_pending or deadline_ns < eligible_ns
                    or (not self._updated.is_set() and self._boat_input is None)):
                continue
            waited_for_action = self._action_lock.locked()
            async with self._action_lock:
                # The reader's cancellation callback may still be waiting to run.
                if self._closing or (reader is not None and reader.cancelled()):
                    return
                if (self._changing_dimension or self._respawn_pending
                        or (self._active_boat() is None and not self._updated.is_set())):
                    continue
                late_ns = perf_counter_ns() - deadline_ns
                if late_ns >= TICK_PERIOD_NS:
                    # The event loop or a player action occupied one or more slots.
                    eligible_ns = deadline_ns + (late_ns // TICK_PERIOD_NS + 1) * TICK_PERIOD_NS
                    continue
                try:
                    if self._input_source is None:
                        pressed = self._next_qa_jump_press()
                        tick = await self._send_input(False, jump_pressed=pressed, jump_edge=not self._jump_was_pressed,
                                                      move_vector=self._manual_directions())
                    else:
                        frame = self._input_source()
                        self._rotation = (0.0, frame.yaw)
                        tick = await self._send_input(frame.distance > 0, supplied=frame)
                    if self._on_input_sent is not None:
                        self._on_input_sent(tick, perf_counter_ns())
                except Exception as error:
                    self._qa_jump_presses.clear()
                    self._clock.stop(error)
                    raise
            eligible_ns = deadline_ns + TICK_PERIOD_NS
            overrun_ns = perf_counter_ns() - eligible_ns
            if waited_for_action or overrun_ns >= 0:
                # Give the contended frame a full period before sending again, on the same grid.
                skipped = (overrun_ns + TICK_PERIOD_NS - 1) // TICK_PERIOD_NS
                eligible_ns += (skipped + 1) * TICK_PERIOD_NS

    async def wait_for_input_end(self) -> None:
        """Wait for ticker shutdown and propagate failures without taking ownership of the task."""
        ticker = self._ticker
        if ticker is None:
            raise RuntimeError("movement has not started")
        await asyncio.wait((ticker,))
        if not ticker.cancelled():
            ticker.result()

    def _check_pose_control(self) -> None:
        if self._closing:
            raise ConnectionError("movement is closed")
        if self._input_source is not None:
            raise RuntimeError("the scheduled input source owns movement")

    async def set_gliding(self, gliding: bool, usable_elytra: Callable[[], bool]) -> int | None:
        b = self.packets.protocol.binding
        flag = b.ActorFlags.GLIDING
        generation = self._pose_generation
        self._check_pose_control()
        if gliding and self._fall_velocity is None:
            async with self._action_lock:
                self._check_pose_control()
                if (generation != self._pose_generation or self._respawn_pending or self._changing_dimension
                        or not self._want_pose_change(flag, True)
                        or not usable_elytra() or self._incompatible_pose()):
                    return None
        if gliding and self._fall_velocity is None:
            interruptions = (b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID, b.RespawnPacket.ID)
            try:
                await self.packets.wait_for_packet(
                    (b.CorrectPlayerMovePredictionPacket.ID, *interruptions), timeout=1,
                    match=lambda _: generation != self._pose_generation or self._fall_velocity is not None,
                )
            except TimeoutError:
                return None
        async with self._action_lock:
            self._check_pose_control()
            if (generation != self._pose_generation or not self._can_send_input
                    or not self._ray_pose_current or self._boat_input is not None
                    or not self._want_pose_change(flag, gliding)):
                return None
            if gliding:
                if (self._fall_velocity is None or self._observed_ground is not False or self._jump_was_pressed
                        or not usable_elytra() or self._incompatible_pose()):
                    return None
            return await self._send_pose(flag, gliding, generation)

    async def set_crawling(self, crawling: bool, blocks) -> int | None:
        flag = self.packets.protocol.binding.ActorFlags.CRAWLING
        generation = self._pose_generation
        async with self._action_lock:
            self._check_pose_control()
            if (generation != self._pose_generation or not self._can_send_input
                    or not self._ray_pose_current or self._boat_input is not None
                    or not self._want_pose_change(flag, crawling) or self._incompatible_pose()):
                return None
            x, y, z = self._input_position
            x, y, z = math.floor(x), math.floor(y - 1.62 + 0.001), math.floor(z)
            head = (x, y + 1, z)
            suitable_headroom = blocks._has_full_collision(head) if crawling else blocks._stance_clearance(head) is True
            if (self._observed_ground is not True or blocks._stance_support((x, y - 1, z)) is not True
                    or blocks._stance_clearance((x, y, z)) is not True or not suitable_headroom):
                return None
            return await self._send_pose(flag, crawling, generation)

    async def set_sneaking(self, sneaking: bool) -> int:
        """Submit a held sneak/stand state; return the submitted client tick.

        This does not imply that BDS accepted the state. Subsequent input
        frames keep the same held state until another call changes it.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            self._require_input()
            changed = self._sneaking != sneaking
            self._sneaking = sneaking
            return await self._send_input(False, sneak_changed=changed, move_vector=self._manual_directions())

    async def set_sprinting(self, sprinting: bool) -> int:
        """Hold or release sprint input for subsequent forward-moving frames.

        Sprinting begins only with forward movement and ends when movement stops
        or sneaking starts. Return the submitted tick, not BDS acceptance.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            self._require_input()
            self._sprint_key = sprinting
            return await self._send_input(False, move_vector=self._manual_directions())

    def _clear_swimming(self) -> None:
        self._swim_epoch += 1
        self._swim_submitted = None
        self._swimming = None
        self._swim_metadata_tick = 0
        self._water_swim_tick = None

    async def set_swimming(
        self, swimming: bool, *, water_contact: Callable[[tuple[float, float, float]], bool | None],
        mounted: Callable[[], bool],
    ) -> int | None:
        epoch = self._swim_epoch
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            if self._input_position is None and not self._respawn_pending:
                await self.wait_for_position_sync()
            if epoch != self._swim_epoch:
                raise RuntimeError("player position changed before submitting swim input")
            if self._respawn_pending:
                return None
            if self._input_source is not None or mounted():
                raise RuntimeError("another input source owns movement")
            if self._swim_submitted is None:
                if bool(self._swimming) == swimming:
                    return None
            else:
                tick, submitted = self._swim_submitted
                # An older or untimed flag can still be observed independently.
                if submitted == swimming and (self._swim_metadata_tick < tick or self._swimming == swimming):
                    return None
            if swimming:
                if (not self._ray_pose_current or not all(map(math.isfinite, self._input_position))
                        or water_contact(self._input_position) is not True):
                    return None

            return await self._submit_swim(swimming, self._manual_directions())

    async def _submit_swim(self, swimming, directions):
        epoch = self._swim_epoch
        tick = await self._send_input(False, swim_change=swimming, move_vector=directions)
        if epoch == self._swim_epoch:
            self._swim_submitted = (tick, swimming)
        return tick

    def _manual_directions(self) -> tuple[float, float] | None:
        target = self._move_target
        if target is None:
            return self._manual_vector
        x, y, z = self._input_position
        dx, dz = target[0] - x, target[2] - z
        if (self._observed_ground is False or abs(y - 1.62 - target[1]) > 0.05
                or (math.hypot(dx, dz) < 0.01
                    and all(abs(value) < _REST_SPEED for value in self._velocity))):
            self._move_target = None
            return self._manual_vector
        return self._ground_target_directions(dx, dz, math.radians(self._rotation[1])) or self._manual_vector

    def _ground_target_directions(self, dx: float, dz: float, yaw: float) -> tuple[float, float] | None:
        scale = 0.3 if self._sneaking else 1.0
        acceleration = self._movement_speed * _GROUND_ACCELERATION * scale
        if acceleration <= 0:
            return None
        # Steer for the total remaining ground travel, including the coasting
        # distance of the velocity carried into this input.
        ax = (dx * (1 - _GROUND_FRICTION) - self._velocity[0]) / acceleration
        az = (dz * (1 - _GROUND_FRICTION) - self._velocity[2]) / acceleration
        cap = max(1.0, math.hypot(ax, az))
        return ((ax * math.cos(yaw) + az * math.sin(yaw)) / cap,
                (-ax * math.sin(yaw) + az * math.cos(yaw)) / cap)

    def _clear_manual_input(self) -> None:
        self._manual_vector = None
        self._move_target = None
        self._qa_jump_presses.clear()

    def _queue_qa_jump_presses(self, presses: list[bool]) -> None:
        self._require_input()
        ticker = self._ticker
        if ticker is None:
            raise RuntimeError("movement has not started")
        # The reader can finish before its shutdown callback stops the clock.
        reader = self.packets._reader_task
        if reader is None:
            raise RuntimeError("packet receiver has not started")
        if reader.done():
            if reader.cancelled():
                raise ConnectionError("packet stream closed")
            reader.result()
            raise ConnectionError("packet reader stopped")
        self._clock.get_tick()
        if ticker.done():
            if not ticker.cancelled():
                ticker.result()
            raise ConnectionError("scheduled input stopped")
        if self._input_source is not None:
            raise RuntimeError("the scheduled input source owns movement")
        self._require_ground_input()
        starts_sequence = not self._qa_jump_presses
        self._qa_jump_presses.extend((pressed, starts_sequence and index == 0)
                                     for index, pressed in enumerate(presses))

    def _next_qa_jump_press(self) -> bool:
        if not self._qa_jump_presses:
            return False
        if self._awaiting_dismount_pose or (self._entities is not None and self._entities.vehicle() is not None):
            self._qa_jump_presses.clear()
            return False
        pressed, starts_sequence = self._qa_jump_presses[0]
        # A new sequence can arrive while the preceding action still holds jump.
        if starts_sequence and pressed and self._jump_was_pressed:
            return False
        self._qa_jump_presses.popleft()
        return pressed

    async def set_move_vector(self, strafe: float, forward: float) -> None:
        """Hold a local direction on scheduled frames, with positive left and forward."""
        if not all(math.isfinite(value) and abs(value) <= 1 for value in (strafe, forward)):
            raise ValueError("strafe and forward must be finite and between -1 and 1")
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            if self._input_source is not None:
                raise RuntimeError("the scheduled input source owns movement")
            self._require_ground_input()
            self._move_target = None
            self._manual_vector = (float(strafe), float(forward))

    async def set_boat_input(self, *, forward: bool, turn: Literal["left", "right"] | None) -> None:
        if not isinstance(forward, bool):
            raise TypeError("boat forward input must be boolean")
        if turn not in (None, "left", "right"):
            raise ValueError("boat turn must be left, right, or None")
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            if self._input_source is not None:
                raise RuntimeError("the scheduled input source owns movement")
            boat = self._observed_driver_boat()
            if boat is None:
                raise RuntimeError("boat input requires an observed driver seat")
            directions = (1.0 if turn == "left" else -1.0 if turn == "right" else 0.0, float(forward))
            if self._active_boat() is None:
                self._unsubscribe_boat = self._entities.subscribe_vehicle_links(
                    self._clear_boat_input, rider_entity_id=self._entities._own_entity_id, vehicle_entity_id=None,
                )
                self._clear_manual_input()
                self._velocity = (0.0, 0.0, 0.0)
                self._finish_jump()
                self._pending_landing = None
                self._sprint_jump_extra = (0.0, 0.0)
                self._last_correction_tick = self._tick - 1
                self._boat_input = _BoatInput(boat.entity_id, boat.runtime_id, directions, self._tick - 1)
            else:
                self._boat_input.directions = directions

    async def move_to(self, position: tuple[float, float, float]) -> None:
        """Set a same-level feet target for the scheduled ordinary movement."""
        target = tuple(map(float, position))
        if len(target) != 3 or not all(map(math.isfinite, target)):
            raise ValueError("move_to requires three finite coordinates")
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            if self._input_source is not None:
                raise RuntimeError("the scheduled input source owns movement")
            self._require_ground_input()
            if self._input_position is None:
                await self.wait_for_position_sync()
            x, y, z = self._input_position
            feet_y = y - 1.62
            if self._observed_ground is False or abs(feet_y - target[1]) > 0.05:
                raise ValueError(
                    f"move_to requires a target on the current ground level "
                    f"(feet Y={feet_y}, target Y={target[1]}, on_ground={self._observed_ground})"
                )
            if target != self._move_target:
                dx, dz = target[0] - x, target[2] - z
                if dx or dz:
                    self._rotation = (self._rotation[0], math.degrees(math.atan2(-dx, dz)))
            self._manual_vector = (0.0, 0.0)
            self._move_target = target

    async def jump(self) -> int:
        """Submit a one-tick jump press and predicted vertical motion.

        Return the submitted tick, not proof of BDS displacement. The idle
        input stream continues the trajectory and releases the press.
        """
        async with self._action_lock:
            self._require_ground_input()
            if self._input_position is None:
                await self.wait_for_position_sync()
            return await self._send_input(False, jump_pressed=True, move_vector=self._manual_directions())

    async def set_flying(self, flying: bool, state: PlayerState, *, require_permission: bool = True) -> None:
        """Submit the jump-key double tap or return if the current target matches.

        The server decides whether the switch takes effect.
        """
        authorization_revision = state.flight_authorization_revision

        def require_same_authorization():
            if authorization_revision != state.flight_authorization_revision:
                raise PermissionError("flight authorization changed during flight switch")

        def before_send():
            require_same_authorization()
            state.flight_target(require_permission=require_permission)

        if require_permission:
            state.flight_target(require_permission=True)
        async with self._action_lock:
            require_same_authorization()
            await self._switch_flight(flying, state, require_permission, before_send, require_same_authorization)

    async def _switch_flight(self, flying, state, require_permission, before_send, check_authorization,
                             *, validate_step=None):
        target, _ = state.flight_target(require_permission=require_permission)
        if target == flying:
            return
        if self._input_position is None:
            await self.wait_for_position_sync()
        for pressed, switch in ((True, None), (False, None), (True, flying)):
            check_authorization()
            target, revision = state.flight_target(require_permission=require_permission)
            if target == flying:
                return
            tick = await self._send_input(False, jump_pressed=pressed, flight_switch=switch,
                                          move_vector=self._manual_directions(), before_send=before_send,
                                          validate_step=validate_step)
            # An input already in the transport can finish after authorization changes.
            check_authorization()
            if switch is None:
                await self._clock.wait_ticks(1)
        state.flight_submitted(flying, revision)
        self._flight_start_tick = tick if flying else None

    def _flight_directions(self, dx, dz, tolerance, yaw):
        vx, _, vz = self._velocity
        coast = (vx / _FLIGHT_HORIZONTAL_BRAKE, vz / _FLIGHT_HORIZONTAL_BRAKE)
        if math.hypot(dx - coast[0], dz - coast[1]) <= tolerance / 2:
            return 0.0, 0.0
        # Choose the impulse that would stop on the target if the next input
        # were neutral. Large errors use the full directional input.
        travel = 1 + _FLIGHT_AIR_DRAG / _FLIGHT_HORIZONTAL_BRAKE
        ax, az = dx / travel - vx, dz / travel - vz
        limit = max(_FLIGHT_HORIZONTAL_ACCELERATION, math.hypot(ax, az))
        sine, cosine = math.sin(yaw), math.cos(yaw)
        strafe = (ax * cosine + az * sine) / limit
        forward = (-ax * sine + az * cosine) / limit
        return (strafe if abs(strafe) > 0.01 else 0.0,
                forward if abs(forward) > 0.01 else 0.0)

    def _flight_input(self, dx, dy, dz, horizontal_tolerance, vertical_tolerance):
        yaw = math.radians(self._rotation[1])
        vector = self._flight_directions(dx, dz, horizontal_tolerance, yaw)
        neutral = _FlightInput(vector, 0, yaw)

        def vertical_error(frame):
            delta, velocity = frame.step(self._velocity)
            return abs(dy - delta[1] - velocity[1] * _FLIGHT_IDLE_DAMPING / _FLIGHT_VERTICAL_BRAKE)

        if vertical_error(neutral) <= vertical_tolerance / 2:
            return neutral
        release_tick = self._jump_release_clock_tick
        may_press = (self._jump_was_pressed or release_tick is None
                     or self._clock.tick - release_tick >= _FLIGHT_JUMP_RELEASE_TICKS)
        choices = (neutral, _FlightInput(vector, -1, yaw))
        if may_press:
            choices += (_FlightInput(vector, 1, yaw),)
        return min(choices, key=vertical_error)

    def _flight_braking_clearance(self, blocks, feet, command):
        # After this command the action may have to release both axes. Check
        # the submitted step and the remaining coast before pressing a key.
        neutral = _FlightInput()
        velocity = self._velocity
        known = True
        for frame in (command,) + (neutral,) * _FLIGHT_BRAKING_TICKS:
            delta, velocity = frame.step(velocity)
            next_feet = tuple(a + b for a, b in zip(feet, delta))
            clear = blocks._flight_clearance(feet, next_feet)
            if clear is False:
                return False
            known = known and clear is True
            feet = next_feet
            if math.hypot(*velocity) < 0.0001:
                break
        return True if known else None

    async def fly_to(self, blocks, state: PlayerState, feet_position, *,
                     horizontal_tolerance: float, vertical_tolerance: float, ticks: int) -> FlightResult:
        target = tuple(map(float, feet_position))
        if len(target) != 3 or not all(map(math.isfinite, target)):
            raise ValueError("fly_to requires three finite feet coordinates")
        if not all(math.isfinite(value) and value > 0 for value in (horizontal_tolerance, vertical_tolerance)):
            raise ValueError("flight tolerances must be finite and positive")
        if type(ticks) is not int or ticks <= 0:
            raise ValueError("flight ticks must be a positive integer")
        self._check_pose_control()
        state.flight_target(require_permission=True)
        if not state._flight_in_creative:
            raise PermissionError("fly_to requires ordinary Creative mode")
        authorization = state.flight_authorization_revision
        generation = self._pose_generation
        async with self._action_lock:
            self._check_pose_control()
            if generation != self._pose_generation or authorization != state.flight_authorization_revision:
                return FlightResult("stopped", 0, self.state.feet_position)
            self._require_ground_input()
            flags = self.packets.protocol.binding.ActorFlags

            def incompatible_pose():
                return (self._incompatible_pose() or self._actor_flag(flags.GLIDING)
                        or self._actor_flag(flags.CRAWLING))

            if self._sneaking or self._sprint_key or incompatible_pose():
                raise RuntimeError("fly_to requires the ordinary standing controls")
            first_tick = self._tick
            deadline = self._clock.get_tick() + ticks
            teleport = self._teleport_pending
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            status = None
            cruising = False

            def check_authorization():
                if generation != self._pose_generation or authorization != state.flight_authorization_revision:
                    raise _FlightInterrupted("stopped")

            def guard():
                if self._closing:
                    raise ConnectionError("movement closed during flight")
                check_authorization()
                if (self._stop_requested or self._changing_dimension
                        or self._teleport_pending not in (None, teleport)
                        or not state._flight_in_creative
                        or incompatible_pose()
                        or (self._entities is not None and self._entities.vehicle() is not None)):
                    raise _FlightInterrupted("stopped")
                if self._clock.get_tick() >= deadline:
                    raise _FlightInterrupted("budget")

            def current_feet():
                feet = self.state.feet_position
                if feet is None or not all(map(math.isfinite, feet)):
                    raise _FlightInterrupted("unknown")
                return feet

            def require_clearance(start, end):
                clear = blocks._flight_clearance(start, end)
                if clear is not True:
                    raise _FlightInterrupted("blocked" if clear is False else "unknown")

            def validate_takeoff_step(origin, next_pos):
                x, y, z = origin
                nx, ny, nz = next_pos
                require_clearance((x, y - 1.62, z), (nx, ny - 1.62, nz))

            try:
                guard()
                feet = current_feet()
                flying, _ = state.flight_target(require_permission=True)
                takeoff_height = feet[1] + _FLIGHT_TAKEOFF_CLEARANCE

                def guard_takeoff():
                    guard()
                    x, y, z = current_feet()
                    require_clearance((x, y, z), (x, max(y, takeoff_height), z))

                if flying:
                    require_clearance(feet, feet)
                else:
                    guard_takeoff()
                self._clear_manual_input()
                if not flying:
                    # Release a previous jump press before a new double tap.
                    if self._jump_was_pressed:
                        await self._send_input(False, before_send=guard_takeoff, validate_step=validate_takeoff_step)
                        await self._clock.wait_ticks(1)
                    await self._switch_flight(True, state, True, guard_takeoff, check_authorization,
                                              validate_step=validate_takeoff_step)
                    await self._clock.wait_ticks(1)
                while state._observed_flying is not True:
                    guard()
                    if state.flight_target(require_permission=True)[0] is not True:
                        raise _FlightInterrupted("stopped")
                    start_tick = self._flight_start_tick
                    if start_tick is None:
                        raise _FlightInterrupted("unknown")
                    grounding = self._server_grounding
                    if (grounding is not None and grounding[0] >= start_tick + _FLIGHT_TAKEOFF_OBSERVATION
                            and grounding[1] is False and abs(grounding[3][1]) < _REST_SPEED):
                        break
                    await self._send_input(False, before_send=guard, validate_step=validate_takeoff_step)
                    await self._clock.wait_ticks(1)
                self._finish_jump()
                self._sprint_jump_extra = (0.0, 0.0)
                cruising = True
                status = await self._fly_towards(blocks, state, target, horizontal_tolerance, vertical_tolerance, guard)
            except _FlightInterrupted as interruption:
                status = interruption.args[0]
            finally:
                try:
                    if self._tick > first_tick and status != "predicted_arrived":
                        sneak_changed = self._sneaking
                        self._sneaking = False
                        if self._can_send_input and generation == self._pose_generation:
                            self._finish_jump()
                            self._sprint_jump_extra = (0.0, 0.0)
                            release = None
                            feet = self.state.feet_position
                            if cruising and all(map(math.isfinite, (*feet, *self._velocity))):
                                neutral = _FlightInput()
                                if self._flight_braking_clearance(blocks, feet, neutral) is True:
                                    release = neutral
                            # A cancelled send may have put a jump press on the
                            # wire before its local key state could be committed.
                            if status is None:
                                self._jump_was_pressed = True
                            try:
                                await self._send_input(False, sneak_changed=sneak_changed, flight=release)
                            except Exception:
                                if status is not None:
                                    raise
                        else:
                            self._jump_was_pressed = False
                finally:
                    self._active_done = None
                    done.set()
            return FlightResult(status, self._tick - first_tick, self.state.feet_position)

    async def _fly_towards(self, blocks, state, target, horizontal_tolerance, vertical_tolerance, guard):
        correction = self._last_correction_tick
        history: dict[int, _FlightInput] = {}
        generation = self._pose_generation
        position_current = True
        hover_at = None
        settled = 0

        def replay_correction():
            nonlocal correction, history
            if correction == self._last_correction_tick:
                return True
            observed = self._server_grounding
            if (observed is None or observed[0] != self._last_correction_tick
                    or generation != self._pose_generation or not self._can_send_input):
                return False
            correction, on_ground, origin, velocity = observed
            if self.position is not origin:
                # A later authoritative position already replaced this correction.
                history.clear()
                return True
            history = {tick: frame for tick, frame in history.items() if tick > correction}
            if on_ground:
                return True
            # Advance only through inputs successfully sent after the corrected tick.
            for frame in history.values():
                delta, velocity = frame.step(velocity)
                origin = tuple(a + b for a, b in zip(origin, delta))
            self._input_position, self._velocity = origin, velocity
            return True

        while True:
            guard()
            if state.flight_target(require_permission=True)[0] is not True or self._observed_ground is True:
                return "stopped"
            if not position_current:
                return "unknown"
            feet = self.state.feet_position
            if feet is None or not all(map(math.isfinite, (*feet, *self._velocity))):
                return "unknown"
            dx, dy, dz = (goal - current for goal, current in zip(target, feet))
            frame = self._flight_input(dx, dy, dz, horizontal_tolerance, vertical_tolerance)
            clear = self._flight_braking_clearance(blocks, feet, frame)
            if clear is not True:
                return "blocked" if clear is False else "unknown"
            vx, vy, vz = self._velocity
            coast = (vx / _FLIGHT_HORIZONTAL_BRAKE, vy * _FLIGHT_IDLE_DAMPING / _FLIGHT_VERTICAL_BRAKE,
                     vz / _FLIGHT_HORIZONTAL_BRAKE)
            at_rest = (not (any(frame.vector) or frame.vertical or self._jump_was_pressed or self._sneaking)
                       and math.hypot(*coast) <= _FLIGHT_HOVER_DRIFT
                       and math.hypot(dx, dz) <= horizontal_tolerance and abs(dy) <= vertical_tolerance
                       and math.hypot(dx - coast[0], dz - coast[2]) <= horizontal_tolerance
                       and abs(dy - coast[1]) <= vertical_tolerance)
            if not at_rest:
                hover_at = None
                settled = 0
            elif hover_at is None or math.dist(hover_at, feet) > _FLIGHT_HOVER_DRIFT:
                hover_at = feet
                settled = 0
            elif settled >= _FLIGHT_HOVER_TICKS:
                return "predicted_arrived"
            descending = frame.vertical < 0
            sneak_changed = descending != self._sneaking
            self._sneaking = descending
            try:
                tick = await self._send_input(False, flight=frame, sneak_changed=sneak_changed, before_send=guard)
                history[tick] = frame
                if at_rest:
                    settled += 1
                await self._clock.wait_ticks(1)
            finally:
                # A timed-out or stopped action releases from the current prediction.
                position_current = replay_correction()

    async def missed_swing(self) -> int:
        """Left-click air: send the attack swing, then one MISSED_SWING input frame.

        Return the submitted client tick, not server acceptance.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            b = self.packets.protocol.binding
            await self.packets.send(animation(b, self.player_id, "SWING", source="ATTACK"))
            return await self._send_input(False, missed_swing=True, move_vector=self._manual_directions())

    async def stop_moving(self) -> None:
        """Release held directions and wait for an active action's release frames."""
        if self._boat_input is not None:
            self._boat_input.directions = (0.0, 0.0)
        done = self._active_done
        if done is not None:
            self._stop_requested = True
            if self._navigation_pose is not None:
                self._navigation_pose.cancel()
            if self._navigation_wake is not None:
                self._navigation_wake.set()
            await done.wait()
        elif self._manual_vector is not None:
            async with self._action_lock:
                if self._manual_vector is not None:
                    self._move_target = None
                    self._manual_vector = (0.0, 0.0)

    async def walk_forward(self, ticks: int = 24) -> int:
        """Submit forward input at 20 Hz, then release it. Return submitted moving frames."""
        return await self._walk(ticks, forward=True)

    async def walk_backward(self, ticks: int = 24) -> int:
        """Submit backward input at 20 Hz without turning; return submitted frames."""
        return await self._walk(ticks, backward=True)

    async def strafe_left(self, ticks: int = 24) -> int:
        """Submit leftward input without turning; return submitted frames."""
        return await self._walk(ticks, strafe=1)

    async def strafe_right(self, ticks: int = 24) -> int:
        """Submit rightward input without turning; return submitted frames."""
        return await self._walk(ticks, strafe=-1)

    async def walk_forward_left(self, ticks: int = 24) -> int:
        """Submit simultaneous forward/left input; return submitted frames."""
        return await self._walk(ticks, forward=True, strafe=1)

    async def walk_forward_right(self, ticks: int = 24) -> int:
        """Submit simultaneous forward/right input; return submitted frames."""
        return await self._walk(ticks, forward=True, strafe=-1)

    async def walk_backward_left(self, ticks: int = 24) -> int:
        """Submit simultaneous backward/left input; return submitted frames."""
        return await self._walk(ticks, backward=True, strafe=1)

    async def walk_backward_right(self, ticks: int = 24) -> int:
        """Submit simultaneous backward/right input; return submitted frames."""
        return await self._walk(ticks, backward=True, strafe=-1)

    async def jump_forward(self, ticks: int = 24) -> int:
        """Jump once while walking forward, then release forward input."""
        return await self._walk(ticks, forward=True, jump_first=True)

    async def hop_forward(self, ticks: int = 32) -> int:
        """Keep walking forward and jump again whenever the predicted player lands.

        Suitable for a prepared level floor; return submitted moving frames,
        not a server-observed landing or number of accepted jumps.
        """
        return await self._walk(ticks, forward=True, repeat_jump=True)

    async def hop_backward(self, ticks: int = 32) -> int:
        """Keep moving backward and jump again after predicted landings.

        Suitable for level ground; return submitted moving frames, not
        server-confirmed jumps or displacement.
        """
        return await self._walk(ticks, backward=True, repeat_jump=True)

    async def hop_left(self, ticks: int = 32) -> int:
        """Keep strafing left and repeat jumps after predicted landings.

        Suitable for prepared level ground; return submitted moving frames.
        """
        return await self._walk(ticks, strafe=1, repeat_jump=True)

    async def hop_right(self, ticks: int = 32) -> int:
        """Keep strafing right and repeat jumps after predicted landings.

        Suitable for prepared level ground; return submitted moving frames.
        """
        return await self._walk(ticks, strafe=-1, repeat_jump=True)

    async def hop_forward_left(self, ticks: int = 32) -> int:
        """Repeat forward-left jumps after predicted landings on level ground."""
        return await self._walk(ticks, forward=True, strafe=1, repeat_jump=True)

    async def hop_forward_right(self, ticks: int = 32) -> int:
        """Repeat forward-right jumps after predicted landings on level ground."""
        return await self._walk(ticks, forward=True, strafe=-1, repeat_jump=True)

    async def hop_backward_left(self, ticks: int = 32) -> int:
        """Repeat backward-left jumps after predicted landings on level ground."""
        return await self._walk(ticks, backward=True, strafe=1, repeat_jump=True)

    async def hop_backward_right(self, ticks: int = 32) -> int:
        """Repeat backward-right jumps after predicted landings on level ground."""
        return await self._walk(ticks, backward=True, strafe=-1, repeat_jump=True)

    async def jump_backward(self, ticks: int = 24) -> int:
        """Jump once while walking backward, then release backward input."""
        return await self._walk(ticks, backward=True, jump_first=True)

    async def jump_forward_left(self, ticks: int = 24) -> int:
        """Jump once with forward/left input; return submitted moving frames."""
        return await self._walk(ticks, forward=True, strafe=1, jump_first=True)

    async def jump_forward_right(self, ticks: int = 24) -> int:
        """Jump once with forward/right input; return submitted moving frames."""
        return await self._walk(ticks, forward=True, strafe=-1, jump_first=True)

    async def jump_backward_left(self, ticks: int = 24) -> int:
        """Jump once with backward/left input; return submitted moving frames."""
        return await self._walk(ticks, backward=True, strafe=1, jump_first=True)

    async def jump_backward_right(self, ticks: int = 24) -> int:
        """Jump once with backward/right input; return submitted moving frames."""
        return await self._walk(ticks, backward=True, strafe=-1, jump_first=True)

    async def jump_left(self, ticks: int = 24) -> int:
        """Jump once while strafing left, then release lateral input."""
        return await self._walk(ticks, strafe=1, jump_first=True)

    async def jump_right(self, ticks: int = 24) -> int:
        """Jump once while strafing right, then release lateral input."""
        return await self._walk(ticks, strafe=-1, jump_first=True)

    async def _walk(self, ticks: int, *, forward: bool = False,
                    backward: bool = False, strafe: int = 0,
                    jump_first: bool = False, repeat_jump: bool = False) -> int:
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            self._require_ground_input()
            self._clear_manual_input()
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                if self._input_position is None:
                    await self.wait_for_position_sync()
                for frame in range(ticks):
                    if self._server_disconnected:
                        raise ConnectionError("server disconnected during movement")
                    if self._stop_requested or self._changing_dimension:
                        break
                    await self._send_input(
                        forward, backward=backward, strafe=strafe,
                        jump_pressed=(jump_first and frame == 0) or (
                            repeat_jump and self._jump_floor_y is None
                            and not self._jump_was_pressed
                        ),
                    )
                    submitted += 1
                    await asyncio.sleep(0.05)
                return submitted
            finally:
                try:
                    await self._release_input()
                finally:
                    self._active_done = None
                    done.set()

    def switch_follow_target(self, runtime_id: int) -> None:
        """Use a new runtime ID on the next active follow frame."""
        if self._follow_target_id is None or self._stop_requested:
            raise RuntimeError("no active entity follow")
        self._follow_target_id = runtime_id

    async def follow_entity(self, entities, runtime_id: int, *, ticks: int = 80,
                            stop_distance: float = 1.5, _resolve=None,
                            _clearance: Callable[[Point, Point], bool | None] | None = None,
                            _start: Callable[[], bool] | None = None) -> int:
        """Track an observed actor for up to ticks frames, releasing input on exit.

        Return submitted moving frames, not a server-confirmed arrival.
        The route is not obstacle-aware; a missing actor ends tracking.
        """
        if not math.isfinite(stop_distance) or stop_distance < 0:
            raise ValueError("stop_distance must be finite and nonnegative")
        resolve = entities.get if _resolve is None else _resolve

        def check_target() -> None:
            if resolve(self._follow_target_id) is None:
                raise _FollowTargetLost

        def validate_step(start: Point, end: Point) -> None:
            feet = (start[0], start[1] - 1.62, start[2])
            next_feet = (end[0], end[1] - 1.62, end[2])
            if _clearance(feet, next_feet) is not True:
                raise _FollowTargetLost

        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            self._require_ground_input()
            self._clear_manual_input()
            self._stop_requested = False
            self._follow_target_id = runtime_id
            done = self._active_done = asyncio.Event()
            submitted = 0
            release = _resolve is None or ticks != 0
            try:
                if not release:
                    return 0
                if self._input_position is None:
                    await self.wait_for_position_sync()
                if _start is not None and (self._stop_requested or not _start()):
                    release = False
                    return 0
                for _ in range(ticks):
                    if self._server_disconnected:
                        raise ConnectionError("server disconnected during movement")
                    if self._stop_requested:
                        break
                    actor = resolve(self._follow_target_id)
                    if actor is None:
                        break
                    x, y, z = self._input_position
                    dx, dz = actor.position[0] - x, actor.position[2] - z
                    forward = math.hypot(dx, dz) > stop_distance
                    if forward:
                        await self.look_at((actor.position[0], y, actor.position[2]))
                    try:
                        if _resolve is None:
                            await self._send_input(forward)
                        else:
                            await self._send_input(
                                forward, before_send=check_target,
                                validate_step=None if _clearance is None else validate_step,
                            )
                    except _FollowTargetLost:
                        break
                    submitted += forward
                    await asyncio.sleep(0.05)
                return submitted
            finally:
                self._follow_target_id = None
                try:
                    if release:
                        await self._release_input()
                finally:
                    self._active_done = None
                    done.set()

    @staticmethod
    def _drop_pickup_clearance(blocks, feet: Point, anchor: Point) -> bool | None:
        if anchor[1] < feet[1]:
            return False
        dx, dz = anchor[0] - feet[0], anchor[2] - feet[2]
        distance = math.hypot(dx, dz)
        # Stop when the anchor is inside the standing player's horizontal footprint.
        scale = 0.0 if distance <= _PLAYER_HALF_WIDTH else 1.0 - _PLAYER_HALF_WIDTH / distance
        return blocks._flight_clearance(feet, (feet[0] + dx * scale, feet[1], feet[2] + dz * scale))

    def _drop_pickup_stance(self, anchor: Point | None) -> Point | None:
        origin = self.input_position
        if anchor is None or origin is None:
            return None
        cell_x, feet_y, cell_z = self._foot_cell(origin)
        if (not self._at_standing_height(feet_y)
                or math.dist((cell_x + 0.5, feet_y, cell_z + 0.5), anchor) > _DROP_PICKUP_RADIUS):
            return None
        return origin[0], feet_y, origin[2]

    async def _navigate_drop(self, blocks, entity_id: int, near_anchor: Callable[[], Point | None],
                             rules: NavigationRules, ticks: int, *, deadline: float | None,
                             on_plan: Callable[[NavigationPlan], None] | None = None) -> NavigationResult:
        return await self._navigate_goal(
            blocks, EntityNearGoal(entity_id, _DROP_PICKUP_RADIUS), rules, ticks, deadline=deadline,
            near_anchor=near_anchor, near_clearance=partial(self._drop_pickup_clearance, blocks), on_plan=on_plan,
        )

    async def _approach_drop(self, blocks, entities: Entities, target: EntityState,
                             resolve: Callable[[int], EntityState | None], *, ticks: int,
                             deadline: float | None, search_radius: int | None = None,
                             on_route: Callable[[], None] | None = None) -> None:
        runtime_id = target.runtime_id
        loop = asyncio.get_running_loop()
        blocked = False
        direct_start_tick = None

        def resolve_in_time(selected: int) -> EntityState | None:
            if not self._stop_requested and (deadline is None or loop.time() < deadline):
                return resolve(selected)
            return None

        def supported_anchor(actor: EntityState | None) -> Point | None:
            anchor = None if actor is None else entities._item_navigation_anchor(actor)
            if anchor is not None:
                x, y, z = map(math.floor, anchor)
                if blocks._stance_support((x, y - 1, z)) is True:
                    return anchor
            return None

        def standing_cell() -> Cell | None:
            origin = self.input_position
            if (origin is None or not all(map(math.isfinite, origin)) or not self._ray_pose_current
                    or not self._navigation_visibility_standing()):
                return None
            cell = self._foot_cell(origin)
            return cell if self._navigation_start_is_standing(blocks, cell) else None

        def direct_is_blocked(step: tuple[Point, Point] | None = None) -> bool:
            if not target.entity_id or (cell := standing_cell()) is None:
                return False
            anchor = supported_anchor(resolve_in_time(runtime_id))
            if anchor is None:
                return False
            x, _, z = self.input_position
            # The swept-cell scan is local even when the caller sets a larger route radius.
            if max(abs(math.floor(anchor[0]) - cell[0]),
                   abs(math.floor(anchor[2]) - cell[2])) > _DROP_DIRECT_PROBE_RADIUS:
                return False
            feet_y = cell[1]
            return (blocks._flight_clearance((x, feet_y, z), (anchor[0], feet_y, anchor[2])) is False
                    or (step is not None and step[0] != step[1] and blocks._flight_clearance(*step) is False))

        def start_direct() -> bool:
            nonlocal blocked, direct_start_tick
            direct_start_tick = self._tick
            blocked = direct_is_blocked()
            return not blocked

        def direct_clearance(start: Point, end: Point) -> bool:
            nonlocal blocked
            # The same drop can move behind a wall after the initial clearance check.
            blocked = direct_is_blocked((start, end))
            return not blocked and (deadline is None or loop.time() < deadline)

        await self.follow_entity(entities, runtime_id, ticks=ticks, stop_distance=0.0,
                                 _resolve=resolve_in_time, _clearance=direct_clearance, _start=start_direct)
        if not blocked or resolve_in_time(runtime_id) is None:
            return
        assert direct_start_tick is not None
        remaining = ticks - (self._tick - direct_start_tick)
        if remaining <= 0:
            return

        if on_route is not None:
            on_route()
        rules = NavigationRules() if search_radius is None else NavigationRules(search_radius=int(search_radius))
        route_start_tick = None

        def record_start(_plan: NavigationPlan) -> None:
            nonlocal route_start_tick
            if route_start_tick is None:
                route_start_tick = self._tick

        route = await self._navigate_drop(
            blocks, target.entity_id, lambda: supported_anchor(resolve_in_time(runtime_id)), rules, remaining,
            deadline=deadline, on_plan=record_start,
        )
        if route.status != "reached":
            return
        if route_start_tick is not None:
            remaining -= self._tick - route_start_tick
        if remaining <= 0 or resolve_in_time(runtime_id) is None:
            return

        def resolve_near(selected: int) -> EntityState | None:
            actor = resolve_in_time(selected)
            if actor is None:
                return None
            cell = standing_cell()
            if cell is None or blocks._flat_stance(cell, allow_vegetation=True) is not True:
                return None
            anchor = supported_anchor(actor)
            feet = self._drop_pickup_stance(anchor)
            return actor if feet is not None and self._drop_pickup_clearance(blocks, feet, anchor) is True else None

        await self.follow_entity(entities, runtime_id, ticks=remaining, stop_distance=_PLAYER_HALF_WIDTH,
                                 _resolve=resolve_near, _clearance=blocks._flight_clearance)


    @classmethod
    def _find_flat_route(cls, start, goals, clear, origin, radius):
        def neighbors(cell):
            for dx, dz in _HORIZONTAL_STEPS:
                neighbor = cell[0] + dx, cell[1] + dz
                if (abs(neighbor[0] - origin[0]) <= radius
                        and abs(neighbor[1] - origin[1]) <= radius and clear(neighbor)):
                    yield neighbor

        return cls._find_route(start, goals, neighbors)

    @staticmethod
    def _find_route(start, goals, neighbors):
        queue = deque([start])
        previous = {start: None}
        while queue:
            current = queue.popleft()
            if current in goals:
                route = []
                while previous[current] is not None:
                    route.append(current)
                    current = previous[current]
                return list(reversed(route))
            for neighbor in neighbors(current):
                if neighbor not in previous:
                    previous[neighbor] = current
                    queue.append(neighbor)
        return None

    async def navigate_to(self, blocks, target: tuple[float, float, float], *,
                          ticks: int = 160, search_radius: int = 8) -> int:
        """Walk to a block's center on observed flat ground.

        Unknown cells are impassable. Return submitted moving input frames,
        not a server-confirmed arrival. On 2225, enter only cells classified as
        safe standing terrain by goal navigation. Stop/cancellation releases input.
        """
        async with self._action_lock:
            if self._closing:
                raise ConnectionError("movement is closed")
            self._require_ground_input()
            if self._input_position is None:
                await self.wait_for_position_sync()
            x, y, z = self._input_position
            # PlayerAuthInput and MovePlayer positions are at eye height.
            feet_y = math.floor(y - 1.62 + 0.001)
            if math.floor(target[1]) != feet_y:
                raise ValueError("flat navigation cannot change elevation")
            start = (math.floor(x), math.floor(z))
            goal = (math.floor(target[0]), math.floor(target[2]))

            def clear(cell):
                x, z = cell
                if self.packets.protocol.number == 2225:
                    standing = blocks._flat_stance((x, feet_y, z), allow_vegetation=True)
                    if cell != start or standing is True:
                        return standing is True
                # Keep the original air check at the start so the bot can leave a hazard.
                body = blocks.observed_block((x, feet_y, z))
                head = blocks.observed_block((x, feet_y + 1, z))
                floor = blocks.observed_block((x, feet_y - 1, z))
                return (body is not None and body.is_air is True
                        and head is not None and head.is_air is True
                        and floor is not None and floor.is_air is False)

            if not clear(start):
                raise RuntimeError("starting terrain is not known walkable")
            route = self._find_flat_route(start, {goal}, clear, start, search_radius)
            if route is None:
                raise RuntimeError("no route through observed flat terrain")
            self._clear_manual_input()
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                for cell_x, cell_z in route:
                    while submitted < ticks:
                        if self._server_disconnected:
                            raise ConnectionError("server disconnected during movement")
                        if self._stop_requested:
                            return submitted
                        px, py, pz = self._input_position
                        waypoint = (cell_x + 0.5, py, cell_z + 0.5)
                        if math.hypot(waypoint[0] - px, waypoint[2] - pz) <= 0.12:
                            break
                        # Do not enter a cell that changed or became unknown.
                        if not clear((cell_x, cell_z)):
                            return submitted
                        await self.look_at(waypoint)
                        await self._send_input(True)
                        submitted += 1
                        await asyncio.sleep(0.05)
                    else:
                        return submitted
                return submitted
            finally:
                try:
                    await self._release_input()
                finally:
                    self._active_done = None
                    done.set()

    @staticmethod
    def _foot_cell(position: tuple[float, float, float]) -> Cell:
        x, y, z = position
        return math.floor(x), round(y - 1.62), math.floor(z)

    def _at_standing_height(self, feet_y: float, *, tolerance: float = 0.05) -> bool:
        return (self._jump_floor_y is None
                and (self._server_grounding is None or self._server_grounding[1])
                and abs(self._input_position[1] - (feet_y + 1.62)) <= tolerance)

    def _walking_height(self, blocks, cell: Cell, grass_path_walking) -> bool:
        if grass_path_walking and blocks._dry_grass_path_stance(cell) is True:
            # Stock BDS reports either grounded Y on a dry path. Do not modify
            # the input origin or accept this lower height on ordinary blocks.
            return (grass_path_walking() and self._observed_ground is True
                    and (self._at_standing_height(cell[1], tolerance=0.001)
                         or self._at_standing_height(cell[1] - 1 / 16, tolerance=0.001)))
        return self._at_standing_height(cell[1])

    def _navigation_start_is_standing(self, blocks, cell: Cell, grass_path_walking=None) -> bool:
        if not self._walking_height(blocks, cell, grass_path_walking):
            return False
        if self._pending_landing is None:
            return True
        ground_tick = self._ground_correction_tick
        return (ground_tick is not None and ground_tick >= self._pending_landing[0]
                and self._input_position == self.position
                and blocks._flat_stance(cell, allow_vegetation=True) is True)

    async def _acquire_recovery_stance(self, blocks, mining_epoch: int) -> bool:
        """Acquire the action lock at a standing pose after a routed pickup."""
        after_tick = None
        move_landing = None

        def world_is_current():
            if self._closing:
                raise ConnectionError("movement is closed")
            return blocks._mining_epoch == mining_epoch and not self._changing_dimension

        def landed_by_move():
            return (move_landing is not None and move_landing[0] == self.position
                    and move_landing[1] is self._server_grounding)

        def ready():
            self._require_ground_input()
            position = self._input_position
            if (position is None or not self._can_send_input or not self._ray_pose_current
                    or self._observed_ground is False or not all(map(math.isfinite, position))):
                return False
            cell = self._foot_cell(position)
            if blocks._flat_stance(cell, allow_vegetation=True) is not True:
                return False
            if after_tick is None:
                return self._navigation_start_is_standing(blocks, cell)
            if self.position != position or self.observed_feet_position is None:
                return False
            if landed_by_move():
                return abs(position[1] - (cell[1] + 1.62)) <= 0.05
            # A position-only packet can move the pose onto the floor without
            # establishing that the server says it landed there.
            grounding = self._server_grounding
            return (grounding is not None and grounding[0] > after_tick and grounding[1]
                    and grounding[2] == position and self._navigation_start_is_standing(blocks, cell))

        b = self.packets.protocol.binding

        def observe(raw):
            nonlocal move_landing
            if not world_is_current():
                return True
            if raw.packet_id == b.MovePlayerPacket.ID:
                packet = self.packets.protocol.decode(raw)
                if int(packet.player_id) == self.player_id:
                    tick = int(packet.tick)
                    landed = (int(packet.reset_position) == 0 and not packet.riding_id and packet.on_ground
                              and (tick == 0 or max(after_tick, self._last_correction_tick) < tick < self._tick))
                    move_landing = (self.position, self._server_grounding) if landed else None
            return ready()

        observed = (
            b.CorrectPlayerMovePredictionPacket.ID, b.MovePlayerPacket.ID,
            b.ServerPlayerPostMovePositionPacket.ID, b.UpdateBlockPacket.ID,
            b.UpdateSubChunkBlocksPacket.ID, b.LevelChunkPacket.ID, b.SubChunkPacket.ID,
            b.SetActorLinkPacket.ID, b.ChangeDimensionPacket.ID, b.RespawnPacket.ID, b.DisconnectPacket.ID,
        )
        while world_is_current():
            if ready():
                await self._action_lock.acquire()
                acquired = False
                try:
                    acquired = world_is_current() and ready()
                    if acquired:
                        if landed_by_move():
                            # The newer normal MovePlayer pose supersedes an older airborne prediction.
                            self._server_grounding = None
                            self._finish_jump()
                            self._pending_landing = None
                            self._sprint_jump_extra = (0.0, 0.0)
                        return True
                finally:
                    if not acquired:
                        self._action_lock.release()
            else:
                if after_tick is None:
                    after_tick = self._last_correction_tick
                await self.packets.wait_for_packet(observed, timeout=None, match=observe)
        return False

    def _navigation_visibility_standing(self) -> bool:
        flags = self.packets.protocol.binding.ActorFlags
        return not (self._observed_ground is False or self._sneaking or self._actor_flag(flags.SNEAKING)
                    or self._swimming or self._incompatible_pose() or self._gliding_or_crawling())

    def _navigation_instances(self, *goals: NavigationGoal | None) -> dict[int, object]:
        """Bind visible targets before an action can queue behind other movement."""
        instances: dict[int, object] = {}
        entities = self._entities
        if entities is None:
            return instances

        def bind(goal: NavigationGoal | None) -> None:
            if isinstance(goal, (EntityNearGoal, EntityVisibilityGoal)):
                target = entities._navigation_target(goal.entity_id)
                if target is not None:
                    instances[goal.entity_id] = target[0]
            elif isinstance(goal, (AnyGoal, AllGoal)):
                for child in goal.goals:
                    bind(child)

        for goal in goals:
            bind(goal)
        return instances

    def _navigation_entity_feet(self, entity_id: int, instances: dict[int, object] | None) -> Point | None:
        target = None if self._entities is None else self._entities._navigation_target(entity_id)
        if target is None:
            return None
        instance, feet = target
        if instances is not None and instances.setdefault(entity_id, instance) is not instance:
            return None
        return feet

    def _navigation_goal(
        self, blocks, goal: NavigationGoal, instances: dict[int, object] | None = None, *,
        visibility_budget: _VisibilityScanBudget | None = None,
    ):
        return _resolve_goal(goal, blocks, lambda entity_id: self._navigation_entity_feet(entity_id, instances),
                             visibility_budget)

    def _require_follow_reader(self, failure: Exception | None = None) -> None:
        ended, cause = self.packets._reader_end_result()
        if ended and (failure is None or failure is cause):
            raise ConnectionError("packet stream closed during movement") from cause

    def _standard_motion(self, blocks, *, owns_sprint: bool = False, owns_sneak: bool = False) -> bool:
        # Stock reports a 0.13 movement attribute while this action sprints.
        speed_is_standard = (math.isclose(self._movement_speed, 0.1, rel_tol=0, abs_tol=1e-6)
                             or (owns_sprint and math.isclose(self._movement_speed, 0.13, rel_tol=0, abs_tol=1e-6)))
        if (self._gap_jump_allowed is None or blocks._player_game_type != "SURVIVAL"
                or self._input_source is not None or (self._sneaking and not owns_sneak) or not speed_is_standard):
            return False
        return not self._gliding_or_crawling() and self._gap_jump_allowed()

    def _standard_ground_motion(self, blocks, *, owns_sneak: bool = False) -> bool:
        return (self._standard_motion(blocks, owns_sneak=owns_sneak)
                and not (self._sprint_key or self._sprinting or self._swimming or self._incompatible_pose()))

    def _water_ready(self):
        air = None if self._navigation_air_supply is None else self._navigation_air_supply()
        return air is not None and air >= _WATER_MIN_AIR

    def _grass_path_walking(self, blocks, goal, rules):
        if isinstance(goal, (FootGoal, BlockInteractionGoal)) and not rules.allow_swimming:
            return partial(self._standard_ground_motion, blocks)
        return None

    def plan_navigation(self, blocks, goal: NavigationGoal, rules: NavigationRules) -> NavigationPlan:
        _require_goal(goal)
        if not isinstance(rules, NavigationRules):
            raise TypeError("expected NavigationRules")
        if self._closing:
            raise ConnectionError("movement is closed")
        if not self._can_send_input or not self._ray_pose_current:
            return NavigationPlan(goal, rules, blocks.dimension_id, None, "unknown")
        start = self._foot_cell(self._input_position)
        self._require_ground_input()
        grass_path_walking = self._grass_path_walking(blocks, goal, rules)
        if not self._navigation_start_is_standing(blocks, start, grass_path_walking):
            return NavigationPlan(goal, rules, blocks.dimension_id, start, "unreachable")
        calibrated = self._standard_ground_motion(blocks)
        graph = _StandingGraph(blocks, start, rules, allow_vertical=not (self._sprint_key or self._sneaking),
                               allow_gap=calibrated, allow_stone_ledge_drop=calibrated, allow_ladder=calibrated,
                               allow_lower_slab_lane=calibrated,
                               allow_short_descent=calibrated and isinstance(goal, FootGoal),
                               allow_water=calibrated and rules.allow_swimming and self._water_ready(),
                               grass_path_walking=grass_path_walking)
        current_goal = self._navigation_goal(blocks, goal, visibility_budget=_VisibilityScanBudget(rules.max_expansions))
        return graph.plan(start, current_goal, limit=rules.max_expansions,
                          start_visibility_standing=self._navigation_visibility_standing)

    async def navigate(self, blocks, goal: NavigationGoal, rules: NavigationRules, *,
                       ticks: int, timeout: float, follow: bool = False,
                       on_plan: Callable[[NavigationPlan], None] | None = None,
                       on_event: Callable[[NavigationEvent], None] | None = None) -> NavigationResult:
        _require_goal(goal)
        if not isinstance(rules, NavigationRules):
            raise TypeError("expected NavigationRules")
        if not isinstance(follow, bool):
            raise TypeError("follow must be a bool")
        if follow and not isinstance(goal, EntityNearGoal):
            raise TypeError("follow=True requires an EntityNearGoal")
        if not isinstance(ticks, int) or ticks < 0 or not 0 <= timeout < math.inf:
            raise ValueError("ticks must be a nonnegative integer and timeout finite and nonnegative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if on_event is None:
            return await self._navigate_goal(blocks, goal, rules, ticks, deadline=deadline, follow=follow,
                                             on_plan=on_plan)

        def emit(event: NavigationEvent) -> None:
            try:
                on_event(event)
            except (Exception, asyncio.CancelledError) as error:
                loop.call_exception_handler({
                    "message": "navigation observer failed", "exception": error, "callback": on_event,
                })

        emit(NavigationEvent("started", goal))
        try:
            result = await self._navigate_goal(
                blocks, goal, rules, ticks, deadline=deadline, follow=follow, on_plan=on_plan, emit_event=emit,
            )
        except asyncio.CancelledError:
            emit(NavigationEvent("finished", goal, reason="cancelled"))
            raise
        except Exception:
            emit(NavigationEvent("finished", goal, reason="error"))
            raise
        emit(NavigationEvent("finished", goal, result=result))
        return result

    async def approach_block(self, blocks, position: tuple[int, int, int], *,
                             ticks: int = 160, search_radius: int = 8,
                             vertical_radius: int = 4) -> BlockApproach:
        if not (0 <= ticks < math.inf and 0 <= search_radius < math.inf and 0 <= vertical_radius < math.inf):
            raise ValueError("ticks and search radii must be finite and nonnegative")
        rules = NavigationRules(int(search_radius), int(vertical_radius))
        result = await self._navigate_goal(blocks, BlockFaceGoal(position), rules, ticks, legacy=True)
        status = {"unknown": "unobserved", "budget": "budget_exhausted"}.get(result.status, result.status)
        face = result.matches[0].face if result.matches else None
        return BlockApproach(status, result.frames, result.stand_position, face)

    async def _navigate_goal(self, blocks, goal, rules, ticks, *, deadline=None, legacy=False, preferred_goal=None,
                             follow: bool = False,
                             on_plan: Callable[[NavigationPlan], None] | None = None,
                             emit_event: Callable[[NavigationEvent], None] | None = None,
                             near_anchor: Callable[[], Point | None] | None = None,
                             near_clearance: Callable[[Point, Point], bool | None] | None = None,
                             settle_epoch: int | None = None):
        loop = asyncio.get_running_loop()
        if near_anchor is not None:
            assert isinstance(goal, EntityNearGoal) and preferred_goal is None and not legacy and near_clearance is not None
        instances = self._navigation_instances(goal, preferred_goal)

        try:
            async with asyncio.timeout_at(deadline):
                if settle_epoch is None:
                    await self._action_lock.acquire()
                elif not await self._acquire_recovery_stance(blocks, settle_epoch):
                    return NavigationResult("unknown", 0)
        except TimeoutError:
            return NavigationResult("budget", 0)
        try:
            if self._closing:
                raise ConnectionError("movement is closed")
            if follow:
                self._require_follow_reader()
            self._require_ground_input()
            if self._changing_dimension:
                return NavigationResult("unknown", 0)
            if deadline is not None and loop.time() >= deadline:
                return NavigationResult("budget", 0)
            if follow and goal.entity_id not in instances:
                return NavigationResult("unknown", 0)
            dimension, epoch = blocks.dimension_id, self._dimension_epoch
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            try:
                if self._input_position is None:
                    if legacy and self._pending_landing is not None:
                        return NavigationResult("unreachable", 0)
                    wait_time = 15 if deadline is None else max(0, deadline - loop.time())
                    pose = self._navigation_pose = asyncio.create_task(self.wait_for_position_sync(wait_time))
                    try:
                        await asyncio.wait((pose,))
                        if pose.cancelled() and self._stop_requested:
                            return NavigationResult("stopped", 0)
                        pose.result()
                    except TimeoutError as error:
                        if follow:
                            self._require_follow_reader(error)
                        if legacy:
                            raise
                        return NavigationResult("budget", 0)
                    finally:
                        self._navigation_pose = None
                        if not pose.done():
                            pose.cancel()
                        await asyncio.gather(pose, return_exceptions=True)
                    self._require_ground_input()
                if self._changing_dimension or blocks.dimension_id != dimension or self._dimension_epoch != epoch:
                    return NavigationResult("unknown", 0)
                if not legacy and not self._ray_pose_current:
                    return NavigationResult("unknown", 0)
                start = self._foot_cell(self._input_position)
                grass_path_walking = None if legacy else self._grass_path_walking(blocks, goal, rules)
                if not self._navigation_start_is_standing(blocks, start, grass_path_walking):
                    return NavigationResult("unreachable", 0)
                self._pending_landing = None
                with ExitStack() as subscriptions:
                    wake = None
                    if follow:
                        wake = self._navigation_wake = asyncio.Event()

                        def wake_navigation(_):
                            wake.set()

                        subscriptions.callback(self.packets.subscribe_raw(wake_navigation))
                        subscriptions.callback(self.packets.subscribe_reader_end(wake_navigation))
                    return await self._navigate_from(
                        blocks, goal, rules, ticks, start, dimension, epoch, deadline, legacy,
                        instances=instances, preferred_goal=preferred_goal, on_plan=on_plan, emit_event=emit_event,
                        near_anchor=near_anchor, near_clearance=near_clearance, follow_wake=wake,
                        grass_path_walking=grass_path_walking,
                    )
            finally:
                self._navigation_wake = None
                self._active_done = None
                done.set()
        except Exception as error:
            if follow and not isinstance(error, ConnectionError):
                self._require_follow_reader(error)
            raise
        finally:
            self._action_lock.release()

    async def _navigate_from(self, blocks, goal, rules, ticks, start, dimension, epoch, deadline, legacy, *,
                             instances, preferred_goal=None, on_plan: Callable[[NavigationPlan], None] | None = None,
                             emit_event: Callable[[NavigationEvent], None] | None = None,
                             near_anchor=None, near_clearance=None, follow_wake: asyncio.Event | None = None,
                             grass_path_walking=None):
        calibrated = not legacy and self._standard_ground_motion(blocks)
        graph = _StandingGraph(blocks, start, rules, allow_vertical=not (self._sprint_key or self._sneaking),
                               allow_gap=calibrated, allow_stone_ledge_drop=calibrated, allow_ladder=calibrated,
                               allow_lower_slab_lane=calibrated,
                               allow_short_descent=calibrated and isinstance(goal, FootGoal),
                               allow_water=calibrated and rules.allow_swimming and self._water_ready(),
                               grass_path_walking=grass_path_walking)
        if legacy and not any(map(graph.in_bounds, _block_face_cells(goal.position))):
            return NavigationResult("unreachable", 0)
        self._clear_manual_input()

        loop = asyncio.get_running_loop()
        timeout = asyncio.timeout_at(deadline)
        accepted_level = start[1]
        submitted = expanded = 0
        remaining = ticks
        release_input = False
        release_sneak = False
        use_ground_velocity = not legacy and self._observed_ground is not False
        rolling = False
        used_ascents = set()
        aimed_cell = None
        parked = False
        route = None
        original_goal = goal
        active_plan = None
        pending_error = None

        def outcome(status, cell=None, matches=()):
            return NavigationResult(status, submitted, expanded, cell, matches)

        def invalidate(reason):
            nonlocal route, active_plan
            route = None
            if emit_event is not None and active_plan is not None:
                plan, active_plan = active_plan, None
                emit_event(NavigationEvent("invalidated", original_goal, plan=plan, reason=reason))

        def abandon(status, reason):
            invalidate(reason)
            return outcome(status)

        def at_center(cell, x, z):
            return math.hypot(cell[0] + 0.5 - x, cell[2] + 0.5 - z) <= 0.12

        def horizontal_speed():
            return max(abs(self._velocity[0]), abs(self._velocity[2]))

        def reached_cell(cell, x, z):
            return at_center(cell, x, z) and (not use_ground_velocity or horizontal_speed() < _REST_SPEED)

        def dry_cruise_stance(cell):
            x, y, z = cell
            floor = (x, y - 1, z)
            block = blocks.observed_block(floor)
            state = None if block is None else blocks._vanilla_state(block.runtime_id)
            # These full-height surfaces use the calibrated ordinary ground friction.
            return (state is not None and state.name in ("minecraft:grass_block", "minecraft:stone")
                    and state.unit_cube and not state.unsafe_support
                    and blocks._stance_air(blocks.observed_block(floor, layer=1)) is True
                    and blocks._flat_stance(cell) is True)

        def cruise_target(anchor, route):
            if (not isinstance(original_goal, FootGoal) or not use_ground_velocity or not calibrated
                    or self._observed_ground is not True or not self._standard_ground_motion(blocks)
                    or len(route) < 2):
                return None
            first, second = route[0], route[1]
            dx, dz = first[0] - anchor[0], first[2] - anchor[2]
            if (abs(dx) + abs(dz) == 1 and anchor[1] == first[1] == second[1]
                    and second == (first[0] + dx, first[1], first[2] + dz)
                    and graph.edge(first, second) is True
                    and all(map(dry_cruise_stance, (anchor, first, second)))):
                return second
            return None

        def check_cruise_step(origin, proposal, corridor):
            # Bound one proposed frame plus all subsequent neutral coasting,
            # not just the frame endpoint (or the feet centre).
            if (proposal[1] != origin[1] or self._observed_ground is not True
                    or not self._standard_ground_motion(blocks)):
                raise _StraightWalkUnsafe
            bounds = []
            for axis in (0, 2):
                start = origin[axis]
                end = start + (proposal[axis] - start) / (1 - _GROUND_FRICTION)
                bounds.append(range(math.floor(min(start, end) - _PLAYER_HALF_WIDTH),
                                    math.floor(max(start, end) + _PLAYER_HALF_WIDTH) + 1))
            y = corridor[0][1]
            for x in bounds[0]:
                for z in bounds[1]:
                    cell = (x, y, z)
                    if cell not in corridor or not dry_cruise_stance(cell):
                        raise _StraightWalkUnsafe

        def stance_failure(clear: bool | None, target: bool | None = True):
            if target is False or clear is False:
                return "unreachable"
            if target is None or clear is None:
                return "unknown"
            return None

        def dimension_is_current():
            return not self._changing_dimension and blocks.dimension_id == dimension and self._dimension_epoch == epoch

        def expired():
            return timeout.expired() or (deadline is not None and loop.time() >= deadline)

        visibility_budget = _VisibilityScanBudget(rules.max_expansions, expired)
        visibility_standing = self._navigation_visibility_standing

        def interrupted(*, check_deadline=True):
            if self._server_disconnected:
                raise ConnectionError("server disconnected during movement")
            if follow_wake is not None:
                self._require_follow_reader()
                if self._closing:
                    return outcome("stopped")
            if not dimension_is_current():
                return abandon("unknown", "dimension_changed")
            if self._stop_requested:
                return outcome("stopped")
            if near_anchor is not None and near_anchor() is None:
                return abandon("unknown", "goal_unmatched")
            if follow_wake is not None and self._navigation_entity_feet(goal.entity_id, instances) is None:
                return abandon("unknown", "goal_unmatched")
            self._require_ground_input()
            if check_deadline and expired():
                return outcome("budget")
            return None

        def parked_status():
            result = interrupted(check_deadline=False)
            if result is not None:
                return result
            position = self._input_position
            if position is None or not self._ray_pose_current or not all(map(math.isfinite, position)):
                return abandon("unknown", "position_changed")
            cell = self._foot_cell(position)
            if (not graph.in_bounds(cell) or not self._at_standing_height(cell[1])
                    or not at_center(cell, position[0], position[2])
                    or horizontal_speed() >= _REST_SPEED
                    or self._teleport_pending is not None):
                reason = "position_changed"
            elif (stance := blocks._flat_stance(cell, allow_vegetation=True)) is None:
                return abandon("unknown", "stance_unavailable")
            elif not stance:
                reason = "stance_unavailable"
            elif self._navigation_goal(blocks, goal, instances).check(cell)[0] is not True:
                reason = "goal_unmatched"
            else:
                reason = None
            if expired():
                return outcome("reached", cell) if reason is None else outcome("budget")
            return reason

        def check_near_anchor():
            if near_anchor() is None:
                raise _FollowTargetLost

        send_input = self._send_input if near_anchor is None else partial(self._send_input, before_send=check_near_anchor)

        def can_release_input() -> bool:
            return (self._can_send_input and dimension_is_current() and not self._awaiting_dismount_pose
                    and self._boat_input is None
                    and (self._entities is None or self._entities.vehicle() is None))

        async def release(*, budgeted=False, sneak_changed=False, guard=None) -> bool:
            nonlocal remaining
            neutral = (0.0, 0.0) if use_ground_velocity else None
            for frame in range(2):
                if not can_release_input():
                    break
                if (expired() and not sneak_changed) or (budgeted and remaining <= 0):
                    return False
                if guard is not None and not guard():
                    return False
                await self._send_input(False, move_vector=neutral, sneak_changed=sneak_changed and frame == 0)
                # A release counts against ticks if navigation subsequently continues.
                if not legacy:
                    remaining -= 1
                if frame == 0:
                    await asyncio.sleep(0.05)
            return True

        async def release_at_arrival(cell):
            nonlocal release_input, aimed_cell, route
            moving = horizontal_speed() >= _REST_SPEED
            if release_input or moving or self._teleport_pending is not None or legacy:
                release_input |= moving or legacy
                await release()
                release_input = False
            aimed_cell = cell
            route = None

        @asynccontextmanager
        async def release_on_exit(*, sneak_changed=False, budgeted=False, guard=None):
            nonlocal pending_error
            unwinding_error = False
            try:
                yield
            except (Exception, asyncio.CancelledError) as error:
                unwinding_error = not isinstance(error, _VisibilityBudgetExhausted) or pending_error is error
                if not sneak_changed and unwinding_error and not isinstance(error, asyncio.CancelledError):
                    pending_error = error
                raise
            finally:
                needs_release = release_sneak if sneak_changed else release_input and not release_sneak
                if needs_release:
                    try:
                        await release(sneak_changed=sneak_changed, budgeted=budgeted, guard=guard)
                    except (Exception, asyncio.CancelledError):
                        if not unwinding_error:
                            raise

        async def descend(source, landing):
            nonlocal remaining, submitted, release_input, accepted_level, use_ground_velocity
            buffer = _runout(source, landing)
            short = blocks._descent_landing(buffer) is not True
            dx, dz = landing[0] - source[0], landing[2] - source[2]
            yaw = math.atan2(-dx, dz)
            lip = 0.5 + _PLAYER_HALF_WIDTH
            clearance = 0.5 - _PLAYER_HALF_WIDTH
            strength = 0.12
            # Reserve space for a complete neutral coast at air drag, even
            # though the first neutral frame normally still has ground drag.
            max_step = 0.1 * _GROUND_ACCELERATION * strength / (1 - _GROUND_FRICTION)
            # Continue just beyond whole-body lip clearance before neutral.
            stop_at = lip + 0.035
            margin = 0.005
            side_safe = clearance - margin
            last_safe = 1 + side_safe

            def offsets(pos):
                x, _, z = pos
                x, z = x - source[0] - 0.5, z - source[2] - 0.5
                return x * dx + z * dz, z * dx - x * dz

            def check_short_step(origin, proposal):
                along, across = offsets(origin)
                end, side = offsets(proposal)
                coast = 0.91 / (1 - 0.91)
                delta = end - along
                min_progress = lip + margin if crossed else -side_safe
                if (abs(side) + abs(side - across) * coast >= side_safe
                        or end + max(0, delta) * coast >= last_safe
                        or end + min(0, delta) * coast <= min_progress
                        or delta > max_step + 0.00001):
                    raise _StraightWalkUnsafe

            approach_column = (source[0], source[2])
            columns = {(landing[0], landing[2])}
            if not short:
                columns.add((buffer[0], buffer[2]))
            lower_columns = frozenset(columns)
            lower_y = landing[1] + 1.62
            upper_y = source[1] + 1.62
            entry_tick = self._tick
            step_tick = settle_tick = None
            # Stock BDS can report grounded more than two seconds into this edge.
            deadline = asyncio.get_running_loop().time() + 3.0
            brake = 2 if release_input else 0
            crossed = False
            resting_frames = 0

            while True:
                result = interrupted()
                if result is not None:
                    return result
                if self._teleport_pending is not None and self._tick > entry_tick:
                    return "position_changed"

                x, y, z = self._input_position
                column = (math.floor(x), math.floor(z))
                on_lower = column in lower_columns
                if (not lower_y - 0.05 <= y <= upper_y + 0.05
                        or (not on_lower and (column != approach_column or abs(y - upper_y) > 0.05))):
                    return abandon("unreachable", "position_changed")
                if not on_lower:
                    target = self._navigation_goal(blocks, goal).block_exists if legacy else True
                    status = stance_failure(blocks._flat_stance(source, allow_vegetation=True), target)
                    if status is not None:
                        return abandon(status, "stance_unavailable")
                    off_axis = (abs(x - source[0] - 0.5) if source[0] == landing[0]
                                else abs(z - source[2] - 0.5))
                    if not crossed and off_axis > 0.12:
                        return "position_changed"

                # The launch stance was checked while the player was in the upper column.
                # Do not switch to the low-speed envelope mid-edge if a
                # previously open runout is obstructed during normal descent.
                if short:
                    clear = blocks._short_descent(source, landing, buffer)
                else:
                    clear = blocks._descent_landing(buffer)
                    if clear is True:
                        clear = graph.edge(source, landing, check_descent_source=False)
                if clear is not True:
                    return abandon("unknown" if clear is None else "unreachable", "edge_unavailable")
                if short:
                    if not self._standard_ground_motion(blocks):
                        return abandon("unknown", "edge_unavailable")
                    progress, lateral = offsets(self._input_position)
                    # A newer pose can supersede the earlier landing correction.
                    if (abs(lateral) >= side_safe or progress >= last_safe
                            or (crossed and progress <= lip + margin)):
                        return abandon("unreachable", "position_changed")
                    if step_tick is None:
                        if not at_center(source, x, z) or self._observed_ground is False:
                            return abandon("unknown", "position_changed")
                        use_ground_velocity = True
                    crossed |= progress >= stop_at or y < upper_y - 0.05
                elif on_lower:
                    progress = ((x - source[0] - 0.5) * dx + (z - source[2] - 0.5) * dz)
                    # From a still upper stance, clear the ledge with the whole foot.
                    crossed = (crossed or not use_ground_velocity or y < upper_y - 0.05 or progress >= lip)
                ground_tick = self._ground_correction_tick
                grounded = (ground_tick is not None and step_tick is not None
                            and step_tick <= ground_tick < self._tick
                            and self._input_position == self.position)
                if short:
                    witness = self._server_grounding
                    grounded = False
                    if settle_tick is not None and witness is not None and self._observed_ground is True:
                        tick, on_ground, received, _ = witness
                        along, across = offsets(received)
                        grounded = (settle_tick <= tick < self._tick and on_ground
                                    and abs(received[1] - lower_y) <= 0.05
                                    and lip + margin < along < last_safe and abs(across) < side_safe)
                if grounded and crossed and resting_frames >= 2:
                    if on_lower and abs(y - lower_y) <= 0.05 and (
                            not short or horizontal_speed() < _REST_SPEED):
                        accepted_level = landing[1]
                        return None
                    if not on_lower:
                        self._pending_landing = None
                        return "position_changed"
                if remaining <= 0 or asyncio.get_running_loop().time() >= deadline:
                    return outcome("budget")

                if short and step_tick is None and horizontal_speed() >= _REST_SPEED:
                    brake = max(brake, 1)
                forward = not crossed and brake == 0
                if forward:
                    if short:
                        self._rotation = (self._rotation[0], math.degrees(yaw))
                    else:
                        await self.look_at((landing[0] + 0.5, y, landing[2] + 0.5))
                first_step = forward and step_tick is None
                try:
                    tick = await send_input(
                        forward, landing_target=(landing[1], lower_columns) if first_step else None,
                        move_vector=(0., strength if forward else 0.) if short else None,
                        validate_step=check_short_step if short else None,
                    )
                except _StraightWalkUnsafe:
                    return abandon("unreachable", "position_changed")
                remaining -= 1
                if forward:
                    if first_step:
                        step_tick = tick
                    release_input = True
                    submitted += 1
                else:
                    brake = max(0, brake - 1)
                    if crossed and settle_tick is None:
                        settle_tick = tick
                    resting_frames += int(crossed)
                    if (crossed and resting_frames >= 2) or (not crossed and brake == 0):
                        release_input = False
                await asyncio.sleep(0.05)

        async def ascend(source, landing):
            nonlocal remaining, submitted, release_input, accepted_level, use_ground_velocity
            sx, sy, sz = source
            lx, ly, lz = landing
            dx, dz = lx - sx, lz - sz
            lower_eye, upper_eye = sy + 1.62, ly + 1.62
            entry_tick = self._tick
            deadline = asyncio.get_running_loop().time() + 2.0
            brake = 2 if release_input else 0
            jump_tick = None
            advancing = False
            braking_at_edge = False
            resting_frames = 0

            def raised_surface(x, z):
                if (lx - _PLAYER_HALF_WIDTH < x < lx + 1 + _PLAYER_HALF_WIDTH
                        and lz - _PLAYER_HALF_WIDTH < z < lz + 1 + _PLAYER_HALF_WIDTH
                        and blocks._stance_support((lx, ly - 1, lz)) is True):
                    return upper_eye
                return None

            while True:
                result = interrupted()
                if result is not None:
                    return result
                if self._teleport_pending is not None and self._tick > entry_tick:
                    return "position_changed"
                x, y, z = self._input_position
                column = (math.floor(x), math.floor(z))
                lateral = x - sx - 0.5 if dz else z - sz - 0.5
                progress = (x - sx - 0.5) * dx + (z - sz - 0.5) * dz
                # Keep the 0.6-wide body within the two checked columns.
                if (abs(lateral) > 0.20 or not -0.20 <= progress <= 1.20
                        or not lower_eye - 0.05 <= y <= upper_eye + 0.30):
                    return abandon("unreachable", "position_changed")
                target = self._navigation_goal(blocks, goal).block_exists if legacy else True
                status = stance_failure(graph.edge(source, landing), target)
                if status is not None:
                    return abandon(status, "edge_unavailable")

                ground_tick = self._ground_correction_tick
                if (jump_tick is not None and ground_tick is not None
                        and jump_tick <= ground_tick < self._tick and self._input_position == self.position):
                    if abs(y - upper_eye) > 0.05 or raised_surface(x, z) is None:
                        return abandon("unreachable", "position_changed")
                    if column == (lx, lz):
                        accepted_level = ly
                        use_ground_velocity = True
                        return None
                if remaining <= 0 or asyncio.get_running_loop().time() >= deadline:
                    return outcome("budget")

                jump = jump_tick is None and brake == 0
                if jump:
                    if not at_center(source, x, z):
                        return "position_changed"
                    await self.look_at((lx + 0.5, y, lz + 0.5))
                # Under a qualified low roof, brake once the whole body clears the lip.
                brake_progress = (0.5 + _PLAYER_HALF_WIDTH
                                  if blocks._ascent_low_roof((lx, ly + 2, lz)) is True else 0.7)
                braking_at_edge = (braking_at_edge or progress >= brake_progress
                                   or (advancing and y < upper_eye - 0.0001))
                # From rest, gain the ledge's height before applying forward input.
                forward = (jump_tick is not None and not jump and not braking_at_edge
                           and (advancing or y >= upper_eye))
                tick = await send_input(
                    forward, jump_pressed=jump, jump_surface=raised_surface if jump else None,
                    landing_target=(ly, frozenset({(lx, lz)})) if jump else None,
                )
                remaining -= 1
                if jump:
                    jump_tick = tick
                    used_ascents.add((source, landing))
                    release_input = True
                elif forward:
                    advancing = True
                    release_input = True
                    submitted += 1
                elif jump_tick is None:
                    brake -= 1
                    release_input = brake > 0
                elif braking_at_edge:
                    resting_frames += 1
                    if resting_frames >= 2:
                        release_input = False
                await asyncio.sleep(0.05)

        async def descend_stone_ledge(source, landing):
            nonlocal remaining, submitted, release_input, accepted_level, use_ground_velocity
            sx, sy, sz = source
            lx, ly, lz = landing
            dx, dz = lx - sx, lz - sz
            yaw = math.degrees(math.atan2(-dx, dz))
            lower_eye, upper_eye = ly + 1.62, sy + 1.62
            wire_eye = float(self.packets.protocol.binding.Vec3(y=upper_eye).y)
            wire_step = math.ldexp(1.0, math.frexp(wire_eye)[1] - 24)
            low_columns = frozenset((lx + i * dx, lz + i * dz) for i in range(3))
            entry_tick = self._tick
            step_tick = air_tick = None
            failed_landing = False
            forward_frames = settled_frames = 0
            settled_at = None

            def progress(position):
                return (position[0] - sx - 0.5) * dx + (position[2] - sz - 0.5) * dz

            def in_corridor(position):
                x, y, z = position
                lateral = x - sx - 0.5 if dz else z - sz - 0.5
                return (abs(lateral) <= 0.20 and -0.20 <= progress(position) <= 3.20
                        and lower_eye - 0.05 <= y <= upper_eye + 0.05)

            def on_low_platform(position):
                return position is not None and (math.floor(position[0]), math.floor(position[2])) in low_columns

            def landed(position):
                # Keep the whole footprint past the lip and within the three low columns.
                return (position is not None and in_corridor(position)
                        and progress(position) >= 0.5 + _PLAYER_HALF_WIDTH
                        and abs(position[1] - lower_eye) <= 0.05)

            def observe_descent(tick, on_ground, position, vertical_delta):
                nonlocal air_tick, failed_landing
                if tick is None or step_tick is None or tick < step_tick:
                    return
                if on_ground:
                    if air_tick is not None and not landed(position):
                        failed_landing = True
                elif (vertical_delta < 0 and in_corridor(position) and on_low_platform(position)
                      and position[1] < upper_eye - 0.05):
                    air_tick = tick

            self._observe_travel_position = observe_descent
            try:
                while True:
                    result = interrupted()
                    if result is not None:
                        return result
                    if self._teleport_pending is not None and self._tick > entry_tick:
                        use_ground_velocity = True
                        return "position_changed"
                    if failed_landing:
                        return abandon("unreachable", "position_changed")
                    status = stance_failure(graph.edge(source, landing), self._standard_ground_motion(blocks))
                    if status is not None:
                        return abandon(status, "edge_unavailable")

                    x, y, z = self._input_position
                    if step_tick is None:
                        if (remaining < _STONE_DROP_MIN_TICKS
                                or (deadline is not None and loop.time() + _STONE_DROP_MIN_TICKS * 0.05 > deadline)):
                            return outcome("budget")
                        observed = self.position
                        # Stock BDS need not echo ordinary flat walking. A replan or any new
                        # server position discards the previous walk's authority at the lip.
                        walked_here = source != plan.nodes[0] and observed is walk_origin
                        if (not self._at_standing_height(sy) or self._observed_ground is False or observed is None
                                or not at_center(source, x, z)
                                or (not walked_here and not at_center(source, observed[0], observed[2]))
                                or max(abs(self._velocity[0]), abs(self._velocity[2])) >= _REST_SPEED
                                or y != observed[1]
                                or (self._ground_correction_tick is None and abs(y - wire_eye) > wire_step)):
                            return abandon("unknown", "edge_unavailable")
                        if self._teleport_pending is not None or self._jump_was_pressed:
                            await send_input(False, move_vector=(0.0, 0.0))
                            remaining -= 1
                            await asyncio.sleep(0.05)
                            continue
                        self._rotation = (self._rotation[0], yaw)
                        # Accept corrections during the write, and consume them only after it succeeds.
                        step_tick = self._tick
                        step_tick = await send_input(True, landing_target=(ly, low_columns))
                        use_ground_velocity = False
                        release_input = True
                        forward_frames = 1
                        remaining -= 1
                        submitted += 1
                        await asyncio.sleep(0.05)
                        continue

                    if not in_corridor(self._input_position):
                        return abandon("unreachable", "position_changed")
                    grounding = self._server_grounding
                    if (air_tick is not None and grounding is not None and grounding[1]
                            and air_tick < grounding[0] < self._tick):
                        if self._observed_ground is True and landed(self.position) and self._input_position == self.position:
                            if self.position != settled_at:
                                settled_at, settled_frames = self.position, 0
                            if settled_frames >= _STONE_DROP_SETTLE_FRAMES:
                                accepted_level = ly
                                release_input = False
                                use_ground_velocity = True
                                return None
                        else:
                            settled_at, settled_frames = None, 0
                    else:
                        settled_at, settled_frames = None, 0

                    if remaining <= 0:
                        return outcome("budget")
                    forward = (air_tick is None and forward_frames < _STONE_DROP_FORWARD_FRAMES
                               and progress(self._input_position) < 0.5 + _PLAYER_HALF_WIDTH
                               and y >= upper_eye - 0.05)
                    if forward:
                        self._rotation = (self._rotation[0], yaw)
                    await send_input(forward)
                    remaining -= 1
                    if forward:
                        forward_frames += 1
                        submitted += 1
                    elif settled_at is not None:
                        settled_frames += 1
                    await asyncio.sleep(0.05)
            finally:
                self._observe_travel_position = None

        async def climb_ladder(source, landing):
            nonlocal remaining, submitted, release_input, accepted_level, use_ground_velocity
            sx, sy, sz = source
            lx, ly, lz = landing
            dx, dz = (lx - sx) // _LADDER_STRIDE, (lz - sz) // _LADDER_STRIDE
            ladder_x, ladder_z = sx + _LADDER_APPROACH * dx, sz + _LADDER_APPROACH * dz
            heading = math.degrees(math.atan2(-dx, dz))
            bottom_eye, top_eye = sy + 1.62, ly + 1.62
            wire_eye = float(self.packets.protocol.binding.Vec3(y=bottom_eye).y)
            wire_step = math.ldexp(1.0, math.frexp(wire_eye)[1] - 24)
            wire_top = float(self.packets.protocol.binding.Vec3(y=top_eye).y)
            deck_eye = wire_top - math.ldexp(1.0, math.frexp(wire_top)[1] - 24)
            forward_limit = 38 + 5 * (ly - sy)
            entry_tick = self._tick
            deck = frozenset((lx + i * dx, lz + i * dz) for i in range(_LADDER_DECK_LENGTH))
            first_tick = brake_tick = rise_tick = crest_tick = None
            grounded_at = None
            departed = False
            forward_frames = neutral_frames = settled_frames = 0
            settled_at = None

            def progress(position):
                return (position[0] - ladder_x - 0.5) * dx + (position[2] - ladder_z - 0.5) * dz

            def in_lane(position):
                lateral = position[0] - ladder_x - 0.5 if dz else position[2] - ladder_z - 0.5
                return (abs(lateral) <= 0.20 and -_LADDER_APPROACH - 0.20 <= progress(position)
                        <= _LADDER_DECK_LENGTH + 0.5 - _PLAYER_HALF_WIDTH)

            def on_deck(position):
                return (position is not None and in_lane(position) and progress(position) >= 0.5 + _PLAYER_HALF_WIDTH
                        and abs(position[1] - top_eye) <= 0.05)

            def in_travel(position):
                return in_lane(position) and bottom_eye - 0.05 <= position[1] <= top_eye + 0.35

            def forget_ground():
                nonlocal grounded_at, settled_at, settled_frames
                grounded_at = settled_at = None
                settled_frames = 0

            def observe_climb(tick, on_ground, position, vertical_delta):
                nonlocal rise_tick, crest_tick, grounded_at, departed
                if first_tick is None or (tick is not None and tick < first_tick):
                    return
                along = progress(position)
                # The verified backing begins half a block past the ladder's center.
                # Only the observed footing may establish that the player cleared it.
                if not in_travel(position) or (along > 0.5 - _PLAYER_HALF_WIDTH and position[1] < deck_eye):
                    departed = True
                    return
                if crest_tick is not None and position[1] < top_eye - 0.05:
                    rise_tick = crest_tick = None
                    forget_ground()
                if tick is None:
                    # A position report can invalidate a landing, but cannot confirm it.
                    if on_ground and not on_deck(position):
                        rise_tick = crest_tick = None
                    anchor = settled_at if settled_at is not None else grounded_at
                    if anchor is not None and (on_ground is False or not on_deck(position)
                                               or math.dist(position, anchor) > _LADDER_SETTLE_DISTANCE):
                        forget_ground()
                    return
                if on_ground:
                    if rise_tick is not None and not on_deck(position):
                        departed = True
                    if (brake_tick is not None and tick >= brake_tick
                            and crest_tick is not None and tick > crest_tick and on_deck(position)):
                        if settled_at is not None and math.dist(position, settled_at) > _LADDER_SETTLE_DISTANCE:
                            forget_ground()
                        grounded_at = position
                    else:
                        forget_ground()
                else:
                    forget_ground()
                    if abs(along) <= 0.20:
                        if (rise_tick is None and vertical_delta > 0
                                and bottom_eye + 0.05 < position[1] < top_eye - 0.05):
                            rise_tick = tick
                        elif rise_tick is not None and position[1] >= top_eye - 0.05:
                            # A correction near the crest can arrive on either side of the peak.
                            crest_tick = tick

            self._observe_travel_position = observe_climb
            try:
                while True:
                    result = interrupted()
                    if result is not None:
                        return result
                    if self._teleport_pending is not None and self._tick > entry_tick:
                        use_ground_velocity = True
                        return "position_changed"
                    if departed:
                        return abandon("unreachable", "position_changed")
                    status = stance_failure(graph.edge(source, landing), self._standard_ground_motion(blocks))
                    if status is not None:
                        return abandon(status, "edge_unavailable")
                    predicted = self._input_position
                    x, y, z = predicted
                    if first_tick is None:
                        reserved = forward_limit + _LADDER_NEUTRAL_FRAMES
                        if remaining < reserved or (deadline is not None and loop.time() + reserved * 0.05 > deadline):
                            return outcome("budget")
                        observed = self.position
                        if (not self._at_standing_height(sy) or self._observed_ground is False or observed is None
                                or not at_center(source, x, z) or not at_center(source, observed[0], observed[2])
                                or max(abs(self._velocity[0]), abs(self._velocity[2])) >= _REST_SPEED
                                or y != observed[1]
                                or (self._ground_correction_tick is None and abs(y - wire_eye) > wire_step)):
                            return abandon("unknown", "edge_unavailable")
                        if self._teleport_pending is not None or self._jump_was_pressed:
                            await send_input(False)
                            remaining -= 1
                            await asyncio.sleep(0.05)
                            continue
                        first_tick = self._tick
                        use_ground_velocity = False
                    elif not in_travel(predicted):
                        return abandon("unreachable", "position_changed")

                    if (grounded_at is not None and self._observed_ground is True
                            and on_deck(self.position) and predicted == self.position):
                        if settled_at is None:
                            settled_at, settled_frames = self.position, 0
                        if settled_frames >= _LADDER_SETTLE_FRAMES:
                            accepted_level = ly
                            release_input = False
                            use_ground_velocity = True
                            return None
                    else:
                        settled_at, settled_frames = None, 0
                    if remaining <= 0:
                        return outcome("budget")
                    if neutral_frames >= _LADDER_NEUTRAL_FRAMES:
                        return abandon("unknown", "edge_unavailable")

                    forward = (brake_tick is None and forward_frames < forward_limit
                               and progress(predicted) < _LADDER_RELEASE_PROGRESS)
                    if forward:
                        self._rotation = (0.0, heading)
                    elif brake_tick is None:
                        brake_tick = self._tick
                    await send_input(forward, landing_target=(ly, deck) if forward_frames == 0 else None)
                    remaining -= 1
                    if forward:
                        forward_frames += 1
                        submitted += 1
                        release_input = True
                    else:
                        neutral_frames += 1
                        if neutral_frames >= 2:
                            release_input = False
                        if settled_at is not None:
                            settled_frames += 1
                    await asyncio.sleep(0.05)
            finally:
                self._observe_travel_position = None

        async def descend_ladder(source, landing):
            nonlocal remaining, submitted, release_input, release_sneak, accepted_level, use_ground_velocity
            ladder = _ladder_descent_geometry(source, landing)
            sx, sy, sz = source
            ly = landing[1]
            yaw = math.atan2(-ladder.dx, ladder.dz)
            heading = math.degrees(yaw)
            align_x, align_z = sx + 0.5 + 0.28 * ladder.dx, sz + 0.5 + 0.28 * ladder.dz
            exit_edge = 1 - _LADDER_STRIDE
            top_eye, bottom_eye = sy + 1.62, ly + 1.62
            rung_columns = frozenset({(ladder.rung_x, ladder.rung_z)})
            phase, frames = "align", 0
            hold_tick = lower_tick = None
            hold_height = first_lowering = last_lowering = None
            grounded = invalidated = owns_sneak = False

            def stage(next_phase):
                nonlocal phase, frames
                phase, frames = next_phase, 0

            def in_rung(position):
                x, y, z = position
                lateral, forward = ladder.offsets(x, z)
                return (abs(lateral) <= 0.18 and 0.06 <= forward <= 0.62
                        and bottom_eye < y < top_eye - 0.03)

            def at_base(position):
                x, y, z = position
                lateral, forward = ladder.offsets(x, z)
                return (abs(lateral) <= 0.18 and 0 <= forward <= 0.65
                        and abs(y - bottom_eye) < 0.04 and _block_column(x, z) in rung_columns)

            def observe(tick, on_ground, position, vertical_delta):
                nonlocal hold_height, first_lowering, last_lowering, grounded, invalidated
                if invalidated or hold_tick is None or (tick is not None and tick < hold_tick):
                    return
                if lower_tick is None or (tick is not None and tick < lower_tick):
                    if tick is None:
                        if hold_height is not None and (on_ground is True or not in_rung(position)):
                            invalidated = True
                    elif on_ground or not in_rung(position):
                        invalidated = True
                    elif hold_height is None:
                        hold_height = position[1]
                    # Stock can report a delta of -0.0784 while stationary on the ladder.
                    elif hold_height - position[1] > 0.05 and vertical_delta < -0.10:
                        invalidated = True
                    return

                # Every own position constrains the descent, including reports without a contact bit.
                on_ladder, at_landing = in_rung(position), at_base(position)
                if not (on_ladder or at_landing) or (on_ground is True and not at_landing):
                    invalidated = True
                    return
                if on_ground is False or not at_landing:
                    grounded = False
                if tick is None:
                    return
                if on_ground:
                    grounded = first_lowering is not None and first_lowering - last_lowering >= 0.45
                elif on_ladder:
                    last_lowering = position[1]
                    if first_lowering is None:
                        first_lowering = last_lowering

            # Each additional rung takes about five ticks plus time for a correction.
            lower_frames = 41 + 6 * (sy - ly - 4)
            minimum_ticks = (
                _LADDER_DESCENT_PHASE_FRAMES["approach"] + _LADDER_DESCENT_HOLD_FRAMES
                + lower_frames + 12 + _LADDER_SETTLE_FRAMES
            )
            if (remaining < minimum_ticks
                    or (deadline is not None and loop.time() + minimum_ticks * 0.05 > deadline)):
                return outcome("budget")
            use_ground_velocity = True
            self._observe_travel_position = observe
            try:
                while True:
                    result = interrupted()
                    if result is not None:
                        return result
                    status = stance_failure(graph.edge(source, landing),
                                            self._standard_ground_motion(blocks, owns_sneak=owns_sneak))
                    if status is not None:
                        return abandon(status, "edge_unavailable")
                    if invalidated:
                        return abandon("unreachable", "position_changed")
                    if phase != "align" and self._teleport_pending is not None:
                        return abandon("unknown", "position_changed")

                    predicted = self._input_position
                    x, y, z = predicted
                    lateral, forward = ladder.offsets(x, z)
                    resting = max(abs(self._velocity[0]), abs(self._velocity[2])) < _REST_SPEED
                    directions = (0.0, 0.0)
                    if phase == "align":
                        if (not self._at_standing_height(sy) or self._observed_ground is False
                                or abs(lateral) > 0.20 or not 1.30 <= forward <= 2.20):
                            return abandon("unknown", "position_changed")
                        if self._teleport_pending is None and not self._jump_was_pressed:
                            if resting and abs(lateral) <= 0.02 and 1.70 <= forward <= 1.92:
                                stage("approach")
                            else:
                                directions = self._ground_target_directions(align_x - x, align_z - z, yaw)
                                if directions is None:
                                    return abandon("unknown", "edge_unavailable")
                    if phase == "approach":
                        if abs(lateral) > 0.18 or y < bottom_eye or forward < 0.20:
                            return abandon("unreachable", "position_changed")
                        if forward <= 0.33:
                            # Brake on submitted position. The first airborne correction
                            # can arrive after the first neutral sneak frame.
                            hold_tick = self._tick
                            stage("hold")
                        else:
                            directions = (0.0, -0.15)
                    if phase == "hold" and frames >= _LADDER_DESCENT_HOLD_FRAMES and hold_height is not None:
                        if not in_rung(self.position) or self._observed_ground is not False:
                            return abandon("unknown", "edge_unavailable")
                        lower_tick = self._tick
                        stage("lower")
                    if phase == "lower" and grounded:
                        if not self._observed_ground or not at_base(self.position) or not at_base(predicted):
                            return abandon("unknown", "edge_unavailable")
                        self._observe_travel_position = None
                        stage("exit")
                    if phase in ("exit", "settle"):
                        if (self._observed_ground is not True or not self._at_standing_height(ly)
                                or abs(lateral) > 0.18 or not exit_edge + 0.30 <= forward <= 0.65):
                            return abandon("unreachable", "position_changed")
                        if phase == "exit":
                            if forward <= exit_edge + 0.80:
                                stage("settle")
                            else:
                                directions = (0.0, -0.65)
                        if phase == "settle" and frames >= _LADDER_SETTLE_FRAMES and resting:
                            accepted_level = ly
                            release_input = False
                            return None

                    if remaining <= 0:
                        return outcome("budget")
                    frame_limit = lower_frames if phase == "lower" else _LADDER_DESCENT_PHASE_FRAMES[phase]
                    if frames >= frame_limit:
                        return abandon("unknown", "edge_unavailable")
                    sneak_changed = frames == 0 and phase in ("hold", "lower")
                    if sneak_changed:
                        self._sneaking = phase == "hold"
                        owns_sneak = True
                    self._rotation = (0.0, heading)
                    moving = max(map(abs, directions)) > 0.01
                    release_input |= moving
                    await send_input(
                        False, move_vector=directions, sneak_changed=sneak_changed,
                        landing_target=(ly, rung_columns) if phase == "approach" and frames == 0 else None,
                    )
                    if sneak_changed and phase == "lower":
                        owns_sneak = False
                    remaining -= 1
                    submitted += int(moving)
                    frames += 1
                    await asyncio.sleep(0.05)
            finally:
                self._observe_travel_position = None
                if owns_sneak:
                    self._sneaking = False
                    release_sneak = True

        async def cross_water(source, landing):
            nonlocal remaining, submitted, release_input, use_ground_velocity
            sx, sy, sz = source
            lx, _, lz = landing
            dx, dz = (lx - sx) // _WATER_STRIDE, (lz - sz) // _WATER_STRIDE
            heading = math.degrees(math.atan2(-dx, dz))
            eye_y = sy + 1.62
            wire_eye = float(self.packets.protocol.binding.Vec3(y=eye_y).y)
            height_step = math.ldexp(1.0, math.frexp(wire_eye)[1] - 24)
            lip_distance = _WATER_STRIDE - 0.5
            runout_end = lip_distance + _WATER_DECK_LENGTH - _PLAYER_HALF_WIDTH
            deck = frozenset((lx + dx * n, lz + dz * n) for n in range(_WATER_DECK_LENGTH))
            entry_tick = self._tick
            first_tick = last_sent = swim_tick = tilt_tick = rise_tick = rim_tick = brake_tick = None
            confirmed_swim_tick = None
            settle_ground_tick = None
            air_at_entry = 0
            observation = grounded = None
            departed = False
            owns_sprint = False
            phase, frames = "enter", 0

            def stage(name):
                nonlocal phase, frames
                phase, frames = name, 0

            def progress(position):
                return (position[0] - sx - 0.5) * dx + (position[2] - sz - 0.5) * dz

            def in_lane(position):
                return abs((position[0] - sx - 0.5) * dz - (position[2] - sz - 0.5) * dx) <= 0.20

            def on_deck(position):
                return (position is not None and in_lane(position)
                        and abs(position[1] - eye_y) <= 0.05
                        and lip_distance + _PLAYER_HALF_WIDTH <= progress(position) <= runout_end)

            def observed_water(position):
                x, y, z = position
                return (1.60 <= progress(position) <= lip_distance - 0.29
                        and blocks._still_water((math.floor(x), math.floor(y - 1.62), math.floor(z))) is True)

            def exited_swimming():
                return (confirmed_swim_tick is not None and self._swimming is False
                        and self._swim_metadata_tick > confirmed_swim_tick)

            def observe(tick, on_ground, position, dy):
                nonlocal observation, departed, rise_tick, rim_tick, grounded
                if first_tick is None or (tick is not None and tick < first_tick):
                    return
                along, y = progress(position), position[1]
                if (not in_lane(position) or not -0.20 <= along <= runout_end
                        or not eye_y - 3.05 <= y <= eye_y + 0.70
                        or (along > lip_distance - _PLAYER_HALF_WIDTH + 0.03 and y < eye_y - 0.05)):
                    departed = True
                    return
                if rim_tick is not None and y < eye_y - 0.05:
                    departed = True
                    return
                if grounded is not None and (on_ground is False or not on_deck(position)
                                              or math.dist(grounded[1], position) > 0.035):
                    grounded = None
                if tick is None:
                    return
                observation = (tick, on_ground, position)
                if tilt_tick is not None and tick >= tilt_tick and not on_ground:
                    if dy > 0.05 and lip_distance - 0.36 <= along < lip_distance and y < eye_y - 0.05:
                        rise_tick = tick
                    if (rise_tick is not None and tick > rise_tick and y >= eye_y - 0.05
                            and lip_distance - 0.21 <= along <= lip_distance + 1.5):
                        rim_tick = tick if rim_tick is None else rim_tick
                if (brake_tick is not None and rim_tick is not None and tick >= brake_tick
                        and on_ground and on_deck(position) and grounded is None):
                    grounded = (tick, position)

            self._observe_travel_position = observe
            try:
                while True:
                    result = interrupted()
                    if result is not None:
                        return result
                    if self._teleport_pending is not None and self._tick > entry_tick:
                        return abandon("unknown", "position_changed")
                    if departed:
                        return abandon("unreachable", "position_changed")
                    air = self._navigation_air_supply()
                    status = stance_failure(graph.edge(source, landing), self._standard_motion(blocks, owns_sprint=owns_sprint))
                    if status is not None:
                        return abandon(status, "edge_unavailable")
                    if air is None or air < 80:
                        return abandon("unknown", "edge_unavailable")
                    if first_tick is None:
                        reserved = _WATER_MIN_TICKS
                        if remaining < reserved or (deadline is not None and loop.time() + reserved * 0.05 > deadline):
                            return outcome("budget")
                        observed = self.position
                        x, y, z = self._input_position
                        if (not self._standard_ground_motion(blocks) or not self._water_ready()
                                or not self._at_standing_height(sy) or self._observed_ground is False
                                or observed is None or not at_center(source, x, z)
                                or not at_center(source, observed[0], observed[2])
                                or max(abs(self._velocity[0]), abs(self._velocity[2])) >= _REST_SPEED
                                or y != observed[1]
                                or (self._ground_correction_tick is None and abs(y - wire_eye) > height_step)):
                            return abandon("unknown", "edge_unavailable")
                        if self._teleport_pending is not None or self._jump_was_pressed:
                            await send_input(False, move_vector=(0.0, 0.0))
                            remaining -= 1
                            await asyncio.sleep(0.05)
                            continue
                        first_tick = self._tick
                        use_ground_velocity = True

                    current = observation
                    if current is not None and (last_sent is None or current[0] > last_sent or current[2] != self.position):
                        current = None
                    if (phase == "enter" and current is not None and not current[1] and observed_water(current[2])):
                        air_at_entry = air
                        stage("submerge")
                    if (phase == "submerge" and current is not None and not current[1] and air < air_at_entry
                            and current[2][1] < sy - 0.03 and observed_water(current[2])
                            and max(abs(self._velocity[0]), abs(self._velocity[2])) <= 0.035):
                        stage("prime")
                        self._sprint_key = owns_sprint = True
                    if (phase == "start" and swim_tick is not None and self._swimming is True
                            and self._swim_metadata_tick >= swim_tick):
                        confirmed_swim_tick = self._swim_metadata_tick
                        stage("wall")
                    if (phase == "wall" and self._swimming is True and current is not None and not current[1]
                            and observed_water(current[2]) and progress(current[2]) >= lip_distance - 0.35):
                        stage("rise")
                        tilt_tick = self._tick
                    if phase == "rise" and rim_tick is not None and rim_tick <= last_sent:
                        stage("lip")
                        frames = last_sent - rim_tick
                    if phase == "lip" and frames >= 6:
                        stage("brake")
                        brake_tick = self._tick
                        self._sprint_key = False
                    if (phase == "brake" and grounded is not None and grounded[0] <= last_sent
                            and self._observed_ground is True and exited_swimming()
                            and on_deck(self.position)):
                        settle_ground_tick = grounded[0]
                        stage("settle")
                    if phase == "settle":
                        if (grounded is None or self._observed_ground is not True or not on_deck(self.position)
                                or not exited_swimming()):
                            return abandon("unknown", "edge_unavailable")
                        if grounded[0] != settle_ground_tick:
                            settle_ground_tick = grounded[0]
                            frames = 0
                        if (frames >= 6 and math.isclose(self._movement_speed, 0.1, rel_tol=0, abs_tol=1e-6)
                                and max(abs(self._velocity[0]), abs(self._velocity[2])) < _REST_SPEED):
                            self._pending_landing = None
                            self._rotation = (0.0, heading)
                            release_input = False
                            return None

                    if remaining <= 2:
                        return outcome("budget")
                    if frames >= _WATER_PHASE_TICKS[phase]:
                        return abandon("unknown", "edge_unavailable")
                    forward = (0.45 if phase == "enter" else 1.0 if phase in ("start", "wall", "rise", "lip") else 0.0)
                    self._rotation = (-60.0 if phase in ("rise", "lip", "brake", "settle") else 0.0, heading)
                    if phase == "start" and swim_tick is None:
                        self._water_swim_tick = self._tick
                        swim_tick = last_sent = await self._submit_swim(True, (0.0, forward))
                    else:
                        last_sent = await send_input(False, move_vector=(0.0, forward),
                                                          landing_target=(sy, deck) if last_sent is None else None)
                    remaining -= 1
                    submitted += int(forward > 0)
                    frames += 1
                    release_input = True
                    if phase == "prime":
                        stage("start")
                    else:
                        await asyncio.sleep(0.05)
            finally:
                self._observe_travel_position = None
                self._water_swim_tick = None
                if owns_sprint:
                    self._sprint_key = False
                    # The deadline may end before a neutral frame can be submitted.
                    self._sprint_release_pending = self._sprinting

        async def cross_lower_slab_lane(source, landing):
            nonlocal remaining, submitted, release_input, use_ground_velocity
            sx, sy, sz = source
            lx, _, lz = landing
            dx, dz = (lx - sx) // _LOWER_SLAB_STRIDE, (lz - sz) // _LOWER_SLAB_STRIDE
            heading = math.degrees(math.atan2(-dx, dz))
            bank_eye = sy + 1.62
            slab_eye = bank_eye - 0.5
            wire_eye = float(self.packets.protocol.binding.Vec3(y=bank_eye).y)
            wire_step = math.ldexp(1.0, math.frexp(wire_eye)[1] - 24)
            strip_margin = 0.5 - _PLAYER_HALF_WIDTH
            first_slab_footing = 0.5 + _PLAYER_HALF_WIDTH
            exit_column = _LOWER_SLAB_STRIDE - 0.5
            exit_overlap = exit_column - _PLAYER_HALF_WIDTH
            landing_target = (sy, frozenset({(lx, lz)}))
            proposals = {}
            low_tick = exit_tick = None
            departed = False
            neutral_frames = settled_frames = 0
            settled_at = None

            def progress(position):
                return (position[0] - sx - 0.5) * dx + (position[2] - sz - 0.5) * dz

            def in_travel(position):
                x, y, z = position
                lateral = x - sx - 0.5 if dz else z - sz - 0.5
                return (abs(lateral) <= strip_margin
                        and -strip_margin <= progress(position) <= _LOWER_SLAB_STRIDE + strip_margin
                        and slab_eye - 0.05 <= y <= bank_eye + 0.05)

            def on_exit(position):
                return progress(position) >= exit_column and abs(position[1] - bank_eye) <= 0.05

            def observe(tick, on_ground, position, _vertical_delta):
                nonlocal low_tick, exit_tick, departed
                if departed or not proposals:
                    return
                proposal = self._input_position if tick is None else proposals.get(tick)
                if proposal is None:
                    return
                if (not in_travel(position)
                        or math.hypot(position[0] - proposal[0], position[2] - proposal[2]) > _PLAYER_HALF_WIDTH):
                    departed = True
                    return
                along = progress(position)
                over_slabs = first_slab_footing <= along <= exit_overlap
                at_bank_height = abs(position[1] - bank_eye) <= 0.05
                if exit_tick is not None:
                    departed = on_ground is False or not on_exit(position)
                elif low_tick is not None and on_ground and over_slabs and at_bank_height:
                    # A grounded position report can contradict the low footing without a client tick.
                    departed = True
                if tick is None or not on_ground or departed:
                    return
                if abs(position[1] - slab_eye) <= 0.05:
                    if over_slabs:
                        low_tick = tick
                    else:
                        departed = True
                elif at_bank_height and low_tick is not None and low_tick < tick and along > exit_overlap:
                    # The footprint can step up before its center reaches the exit column.
                    if along >= exit_column:
                        exit_tick = tick
                elif low_tick is not None or not at_bank_height or along > first_slab_footing:
                    departed = True

            def register_step(_origin, position):
                if not in_travel(position):
                    raise _LowerSlabDeparture
                # send() can synchronously dispatch a correction for the tick it is submitting.
                proposals[self._tick] = position

            def can_brake():
                if (departed or self._teleport_pending is not None or graph.edge(source, landing) is not True
                        or not self._standard_ground_motion(blocks)):
                    return False
                x, y, z = self._input_position
                vx, _, vz = self._velocity
                return in_travel((x, y, z)) and in_travel((x + vx, y, z + vz))

            self._observe_travel_position = observe
            try:
                async with release_on_exit(budgeted=True, guard=can_brake):
                    while True:
                        result = interrupted()
                        if result is not None:
                            return result
                        if departed or (proposals and self._teleport_pending is not None):
                            return abandon("unreachable", "position_changed")
                        status = stance_failure(graph.edge(source, landing), self._standard_ground_motion(blocks))
                        if status is not None:
                            return abandon(status, "edge_unavailable")

                        position = self._input_position
                        x, y, z = position
                        vx, _, vz = self._velocity
                        still = max(abs(vx), abs(vz)) < _REST_SPEED
                        if not proposals:
                            if (remaining < _LOWER_SLAB_MIN_TICKS
                                    or (deadline is not None and loop.time() + _LOWER_SLAB_MIN_TICKS * 0.05 > deadline)):
                                return outcome("budget")
                            observed = self.position
                            if (not self._at_standing_height(sy) or self._observed_ground is False or not still
                                    or observed is None or not at_center(source, x, z)
                                    or not at_center(source, observed[0], observed[2]) or y != observed[1]
                                    or (self._ground_correction_tick is None and abs(y - wire_eye) > wire_step)):
                                return abandon("unknown", "edge_unavailable")
                            if self._teleport_pending is not None or self._jump_was_pressed:
                                await send_input(False, move_vector=(0.0, 0.0))
                                remaining -= 1
                                await asyncio.sleep(0.05)
                                continue
                            use_ground_velocity = True
                        elif not in_travel(position):
                            return abandon("unreachable", "position_changed")

                        resting_on_exit = (exit_tick is not None and self._observed_ground is True and still
                                           and on_exit(position) and at_center(landing, x, z))
                        if resting_on_exit:
                            if settled_at is None or math.dist(position, settled_at) > 0.025:
                                settled_at, settled_frames = position, 0
                            if settled_frames >= _LOWER_SLAB_SETTLE_FRAMES:
                                self._pending_landing = None
                                return None
                        else:
                            settled_at, settled_frames = None, 0
                        if remaining <= 0:
                            return outcome("budget")
                        if neutral_frames >= _LOWER_SLAB_NEUTRAL_LIMIT:
                            return abandon("unknown", "edge_unavailable")

                        # Aim for the first bank's center including the distance of an ordinary release.
                        coast = (vx * dx + vz * dz) * _GROUND_FRICTION / (1 - _GROUND_FRICTION)
                        distance = _LOWER_SLAB_STRIDE - progress(position) - coast
                        forward = 0.0 if abs(distance) <= 0.025 else max(-0.22, min(0.55, distance * 1.8))
                        self._rotation = (0.0, heading)
                        await send_input(False, move_vector=(0.0, forward), validate_step=register_step,
                                         landing_target=landing_target if not proposals else None)
                        remaining -= 1
                        submitted += int(abs(forward) > 0.01)
                        release_input = forward != 0.0
                        neutral_frames = 0 if release_input else neutral_frames + 1
                        settled_frames = settled_frames + 1 if resting_on_exit and not release_input else 0
                        await asyncio.sleep(0.05)
            except _LowerSlabDeparture:
                return abandon("unreachable", "position_changed")
            finally:
                self._observe_travel_position = None
                release_input = False

        async def cross_gap(source, landing):
            nonlocal remaining, submitted, release_input, use_ground_velocity
            sx, sy, sz = source
            lx, _, lz = landing
            dx, dz = (lx - sx) // 2, (lz - sz) // 2
            yaw = math.atan2(-dx, dz)
            heading = math.degrees(yaw)
            eye_y = sy + 1.62
            wire_eye_y = float(self.packets.protocol.binding.Vec3(y=eye_y).y)
            wire_height_step = math.ldexp(1.0, math.frexp(wire_eye_y)[1] - 24)
            entry_tick = self._tick
            jump_tick = None
            forward_frames = settled_frames = 0
            settled_at = None
            before_alignment = None
            landing_columns = frozenset({(lx, lz)})

            def in_corridor(x, z):
                ox, oz = x - sx - 0.5, z - sz - 0.5
                progress = ox * dx + oz * dz
                lateral = ox if dz else oz
                return abs(lateral) <= 0.20 and -0.20 <= progress <= 2.20

            def on_landing(position):
                return (position is not None and abs(position[1] - eye_y) <= 0.05
                        and self._foot_cell(position) == landing and in_corridor(position[0], position[2]))

            while True:
                result = interrupted()
                if result is not None:
                    return result
                if self._teleport_pending is not None and self._tick > entry_tick:
                    use_ground_velocity = True
                    return "position_changed"
                status = stance_failure(graph.edge(source, landing), self._standard_ground_motion(blocks))
                if status is not None:
                    return abandon(status, "edge_unavailable")
                x, y, z = self._input_position
                from_center_x, from_center_z = x - sx - 0.5, z - sz - 0.5
                if jump_tick is None:
                    if (remaining < _GAP_MIN_TICKS
                            or (deadline is not None and loop.time() + _GAP_MIN_TICKS * 0.05 > deadline)):
                        return outcome("budget")
                    if (self._foot_cell(self._input_position) != source or not self._at_standing_height(sy)
                            or self._observed_ground is False):
                        return abandon("unreachable", "position_changed")
                    self._rotation = (self._rotation[0], heading)
                    still = (math.hypot(from_center_x, from_center_z) <= 0.05
                             and max(abs(self._velocity[0]), abs(self._velocity[2])) < _REST_SPEED)
                    if not still or self._teleport_pending is not None or self._jump_was_pressed:
                        directions = (0.0, 0.0) if self._teleport_pending is not None or self._jump_was_pressed else (
                            self._ground_target_directions(-from_center_x, -from_center_z, yaw)
                        )
                        if directions is None:
                            return abandon("unreachable", "edge_unavailable")
                        if any(directions) or self._velocity[0] or self._velocity[2]:
                            before_alignment = self.position
                        await send_input(False, move_vector=directions)
                        remaining -= 1
                        moving = max(map(abs, directions)) > 0.01
                        submitted += int(moving)
                        release_input |= moving
                        await asyncio.sleep(0.05)
                        continue
                    # A predicted alignment can settle before BDS corrects the player.
                    observed = self.position
                    if observed is None or observed is before_alignment:
                        return abandon("unknown", "edge_unavailable")
                    if (abs(observed[1] - eye_y) > 0.05
                            or math.hypot(observed[0] - sx - 0.5, observed[2] - sz - 0.5) > 0.05):
                        return abandon("unknown", "position_changed")
                    # Vanilla can report the adjacent float32 value for the stone surface.
                    if y != observed[1] or (self._ground_correction_tick is None
                                           and abs(y - wire_eye_y) > wire_height_step):
                        return abandon("unknown", "edge_unavailable")
                    jump_tick = await send_input(
                        True, jump_pressed=True, landing_target=(sy, landing_columns),
                    )
                    use_ground_velocity = False
                    release_input = True
                    forward_frames = 1
                    remaining -= 1
                    submitted += 1
                    await asyncio.sleep(0.05)
                    continue

                if not in_corridor(x, z) or not eye_y - 0.05 <= y <= eye_y + 1.30:
                    return abandon("unreachable", "position_changed")

                grounding = self._server_grounding
                if grounding is not None and grounding[1] and jump_tick + _GAP_FORWARD_FRAMES <= grounding[0] < self._tick:
                    if not on_landing(grounding[2]):
                        return abandon("unreachable", "position_changed")
                    if self._observed_ground is True and on_landing(self.position) and self._input_position == self.position:
                        if self.position != settled_at:
                            settled_at, settled_frames = self.position, 0
                        if settled_frames >= _GAP_SETTLE_FRAMES:
                            release_input = False
                            use_ground_velocity = True
                            return None
                    else:
                        settled_at, settled_frames = None, 0
                else:
                    settled_at, settled_frames = None, 0

                if remaining <= 0:
                    return outcome("budget")
                forward = forward_frames < _GAP_FORWARD_FRAMES
                if forward:
                    self._rotation = (self._rotation[0], heading)
                await send_input(forward)
                remaining -= 1
                if forward:
                    forward_frames += 1
                    submitted += 1
                elif settled_at is not None:
                    settled_frames += 1
                await asyncio.sleep(0.05)

        # A held sneak release must still be sent after the action deadline.
        async with release_on_exit(sneak_changed=True):
            try:
                async with timeout, release_on_exit():
                    while True:
                        resuming = parked
                        if parked:
                            # Recheck the latest state before sleeping, including updates during input release.
                            follow_wake.clear()
                            status = parked_status()
                            if status is None:
                                await follow_wake.wait()
                                continue
                            if isinstance(status, NavigationResult):
                                return status
                            parked = False
                            aimed_cell = None
                            invalidate(status)

                        result = interrupted()
                        if result is not None:
                            return result
                        x, y, z = self._input_position
                        current = self._foot_cell(self._input_position)
                        current_y = current[1]
                        # Position-only updates cannot turn an airborne correction into a stance.
                        if not self._walking_height(blocks, current, grass_path_walking) or not graph.in_bounds(current):
                            return abandon("unreachable", "position_changed")
                        if self._teleport_pending is not None or resuming:
                            accepted_level = current_y
                            use_ground_velocity |= current_y > start[1]
                        elif current_y != accepted_level:
                            return abandon("unreachable", "position_changed")

                        requested_goal = goal if preferred_goal is None else preferred_goal
                        if near_anchor is None:
                            current_goal = self._navigation_goal(blocks, requested_goal, instances,
                                                                 visibility_budget=visibility_budget)
                        else:
                            current_goal = _StandingNearState(requested_goal, point=near_anchor(),
                                                              clearance=near_clearance)
                        target = current_goal.block_exists if legacy else True
                        status = stance_failure(graph.walk_stance(current), target)
                        if status is not None:
                            if legacy or status == "unreachable":
                                return abandon(status, "stance_unavailable")
                            invalidate("stance_unavailable")
                        elif current == aimed_cell and reached_cell(current, x, z) and self._teleport_pending is None:
                            matches, faces = graph.check_goal(current_goal, current, eye=(x, y, z),
                                                              visibility_standing=visibility_standing)
                            if matches is True:
                                if follow_wake is not None:
                                    parked = True
                                    continue
                                point = _aim_point(faces)
                                if point is not None:
                                    await self.look_at(point)
                                return outcome("reached", current, faces)
                            if route is None:
                                invalidate("goal_unmatched")
                        elif route is None and active_plan is not None:
                            # The pose changed after the tentative arrival and input release.
                            invalidate("position_changed")

                        if (follow_wake is not None and route is not None
                                and self._ground_correction_tick == self._tick - 1
                                and self._input_position is self.position and self._teleport_pending is None
                                and at_center(current, x, z) and horizontal_speed() < _REST_SPEED
                                and current_goal.check(current)[0] is True):
                            # The latest sent frame was corrected to an in-range stance on this leg.
                            await release_at_arrival(current)
                            continue

                        if route is not None:
                            next_cell = route[0]
                            # Corrections can place the player outside the current leg.
                            off_axis = (abs(x - (anchor[0] + 0.5)) if anchor[0] == next_cell[0]
                                        else abs(z - (anchor[2] + 0.5)))
                            if (self._teleport_pending is not None or current not in (anchor, next_cell)
                                    or (anchor != next_cell and off_axis > 0.12)):
                                invalidate("position_changed")
                            elif graph.edge(anchor, next_cell) is not True:
                                invalidate("edge_unavailable")
                            elif graph.check_goal(current_goal, route[-1])[0] is not True:
                                invalidate("goal_unmatched")
                        if route is None:
                            # An invalidation observer can synchronously request a stop.
                            if emit_event is not None and self._stop_requested:
                                return interrupted()
                            limit = None if legacy else rules.max_expansions - expanded
                            start_eye = (x, y, z) if reached_cell(current, x, z) else None
                            plan = graph.plan(current, current_goal, limit=limit, used_ascents=used_ascents,
                                              expired=None if legacy else expired, start_eye=start_eye,
                                              start_visibility_standing=visibility_standing)
                            expanded += plan.expanded
                            search_expired = expired()
                            adopted = (not search_expired and plan.status == "found" and (legacy or remaining >= 0)
                                       and (remaining > 0 or (start_eye is not None and len(plan.nodes) == 1)))
                            if emit_event is not None:
                                if adopted:
                                    active_plan = plan
                                emit_event(NavigationEvent("plan", original_goal, plan=plan, adopted=adopted))
                            if search_expired:
                                return outcome("budget")
                            if preferred_goal is not None:
                                # Choose once from the synchronized pose while this action owns movement.
                                preferred_goal = None
                                if plan.status == "found":
                                    goal = requested_goal
                                elif plan.status != "budget":
                                    continue
                            if plan.status != "found":
                                return outcome(plan.status)
                            if not legacy and remaining < 0:
                                return outcome("budget")
                            if (rolling and self._ground_correction_tick == self._tick - 1
                                    and self._input_position is self.position and self._teleport_pending is None
                                    and self._observed_ground is True and horizontal_speed() < _REST_SPEED):
                                # A fresh stationary correction supersedes the prior coast; centre the new start normally.
                                rolling = False
                            anchor = current
                            walk_origin = (self.position if self._observed_ground is True
                                           and start_eye == self.position else None)
                            route = deque(plan.nodes)
                            notify_plan = on_plan is not None and adopted
                            if notify_plan:
                                try:
                                    on_plan(plan)
                                except (Exception, asyncio.CancelledError) as error:
                                    # Keep an explicit callback failure if the deadline interrupts cleanup.
                                    pending_error = error
                                    raise
                            if emit_event is not None or notify_plan:
                                result = interrupted()
                                if result is not None:
                                    return result
                            # An ascent starts from rest, including after a flat approach to the ledge.
                            use_ground_velocity |= any(cell[1] > current_y for cell in plan.nodes)

                        extra = cruise_target(anchor, route)
                        through_center = extra is not None and current == route[0] and (
                            (x - current[0] - 0.5) * (current[0] - anchor[0])
                            + (z - current[2] - 0.5) * (current[2] - anchor[2]) >= 0
                        )
                        stopped_here = reached_cell(route[0], x, z)
                        ladder_top = False
                        if len(route) > 1 and route[0] == current:
                            ladder = _ladder_descent_geometry(current, route[1])
                            if ladder is not None:
                                lateral, forward = ladder.offsets(x, z)
                                ladder_top = abs(lateral) <= 0.20 and forward >= 1.30
                        if route[0][1] == current_y and (stopped_here or through_center or ladder_top):
                            if stopped_here:
                                rolling = False
                            anchor = route.popleft()
                            if not route:
                                matches, faces = graph.check_goal(current_goal, anchor, eye=(x, y, z),
                                                                  visibility_standing=visibility_standing)
                                if matches is not True:
                                    invalidate("goal_unmatched")
                                    continue
                                point = _aim_point(faces)
                                if point is not None:
                                    await self.look_at(point)
                                if legacy and aimed_cell is not None:
                                    if remaining <= 0:
                                        return outcome("budget")
                                    remaining -= 1
                                await release_at_arrival(anchor)
                                continue
                            if graph.edge(anchor, route[0]) is not True:
                                invalidate("edge_unavailable")
                                continue
                            extra = cruise_target(anchor, route)

                        if _is_gap_jump(anchor, route[0]):
                            transition = cross_gap
                        elif _is_stone_ledge_drop(anchor, route[0]):
                            transition = descend_stone_ledge
                        elif _is_ladder_climb(anchor, route[0]):
                            transition = climb_ladder
                        elif _ladder_descent_geometry(anchor, route[0]) is not None:
                            transition = descend_ladder
                        elif _is_water_crossing(anchor, route[0]):
                            transition = cross_water
                        elif _is_lower_slab_lane(anchor, route[0]):
                            transition = cross_lower_slab_lane
                        elif route[0][1] > anchor[1]:
                            transition = ascend
                        elif route[0][1] < anchor[1]:
                            transition = descend
                        else:
                            transition = None
                        if transition is not None:
                            result = await transition(anchor, route[0])
                            if isinstance(result, str):
                                invalidate(result)
                            elif result is not None:
                                return result
                            elif emit_event is not None and active_plan is not None:
                                emit_event(NavigationEvent("transition_completed", original_goal, plan=active_plan))
                                active_plan = None
                            route = aimed_cell = None
                            continue
                        if remaining <= 0:
                            return outcome("budget")
                        aim = route[0] if extra is None else extra
                        cell_x, _, cell_z = aim
                        if use_ground_velocity:
                            # Keep the observed ground velocity through steering and braking.
                            dx, dz = cell_x + 0.5 - x, cell_z + 0.5 - z
                            yaw = math.atan2(-dx, dz)
                            self._rotation = (self._rotation[0], math.degrees(yaw))
                            directions = self._ground_target_directions(dx, dz, yaw)
                            if directions is None:
                                return abandon("unreachable", "edge_unavailable")
                            rolling |= extra is not None
                            try:
                                await send_input(
                                    False, move_vector=directions,
                                    validate_step=(lambda a, b: check_cruise_step(a, b, (anchor, route[0], aim)))
                                    if rolling else None,
                                )
                            except _StraightWalkUnsafe:
                                return abandon("unknown", "edge_unavailable")
                            submitted += int(max(map(abs, directions)) > 0.01)
                        else:
                            previous_yaw = self._rotation[1]
                            _, yaw = await self.look_at((cell_x + 0.5, y, cell_z + 0.5))
                            if release_input and abs(math.remainder(yaw - previous_yaw, 360)) >= 45:
                                # BDS can carry the previous leg's velocity through a sharp turn.
                                if not await release(budgeted=not legacy):
                                    return outcome("budget")
                                release_input = False
                                continue
                            await send_input(True)
                            submitted += 1
                        release_input = True
                        remaining -= 1
                        aimed_cell = None
                        await asyncio.sleep(0.05)
            except _FollowTargetLost:
                return abandon("unknown", "goal_unmatched")
            except _VisibilityBudgetExhausted as error:
                if pending_error is error:
                    raise
                return outcome("budget")
            except TimeoutError as error:
                if not timeout.expired() or pending_error is error:
                    raise
                if pending_error is not None:
                    raise pending_error from error
                if follow_wake is not None:
                    return parked_status() if parked else interrupted()
                return outcome("budget")

    async def send_item_use(self, transaction) -> int:
        """Serialize an item action in the same client-tick stream as movement."""
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            return await self._send_input(False, item_use_transaction=transaction,
                                          move_vector=self._manual_directions())

    async def send_block_actions(
        self, actions, *, before_send: Callable[[], None] | None = None,
        build_item_use: Callable[[], object] | None = None,
    ) -> int:
        """Submit one or more block actions in a single player input tick."""
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            item_use = None if build_item_use is None else build_item_use()
            return await self._send_input(False, block_actions=actions, before_send=before_send,
                                          item_use_transaction=item_use,
                                          move_vector=self._manual_directions())

    async def aclose(self):
        """Stop active movement and drain the input ticker before the transport closes."""
        ticker = self._ticker
        self.close()
        try:
            async with self._action_lock:
                pass
        finally:
            if ticker is not None:
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._dimensions is not None:
            self._dimensions.close()
        self._clock.stop()
        self._clear_swimming()
        self._stop_requested = True
        if self._navigation_wake is not None:
            self._navigation_wake.set()
        self._clear_manual_input()
        self._finish_jump()
        self._pending_landing = None
        self._clear_boat_input()
        self._server_grounding = None
        self._ground_correction_tick = None
        self._clear_pose_observations()
        self._updated.set()
        if self._ticker is not None:
            self._ticker.cancel()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
