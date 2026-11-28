"""Server-reported vitals, effects and respawn for the local player."""

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from struct import Struct
from typing import Literal

from .protocol import RawPacket


VITAL_ATTRIBUTES = {
    "minecraft:health": "health",
    "minecraft:absorption": "absorption",
    "minecraft:player.hunger": "hunger",
    "minecraft:player.saturation": "saturation",
    "minecraft:player.level": "experience_level",
    "minecraft:player.experience": "experience_progress",
}
# Bedrock movement effects: speed, slowness, jump boost, levitation, slow falling.
_JUMP_MOVEMENT_EFFECTS = frozenset((1, 2, 8, 24, 27))
_FLOAT32 = Struct("<f")
_MAX_EXPERIENCE_LEVEL_2225 = 24791


@dataclass(frozen=True, slots=True)
class PlayerVitals:
    """Latest local-player vitals. None means a field has no usable observation."""

    health: float | None = None
    hunger: float | None = None
    saturation: float | None = None
    air_supply: int | None = None
    air_supply_max: int | None = None
    experience_level: int | None = None
    experience_progress: float | None = None


@dataclass(frozen=True, slots=True)
class ExperienceBalance:
    """Points derived from the latest Bedrock experience attributes."""

    full_level_cost: int
    current_level_points: int | None
    total_points: int | None


def _experience_balance(vitals: PlayerVitals) -> ExperienceBalance | None:
    level = vitals.experience_level
    if type(level) is not int or not 0 <= level <= _MAX_EXPERIENCE_LEVEL_2225:
        return None

    # The balance at the start of this level sums all the preceding full bars.
    if level <= 15:
        cost = 2 * level + 7
        base = level * (level + 6)
    elif level <= 30:
        cost = 5 * level - 38
        base = (level * (5 * level - 81) + 720) // 2
    else:
        cost = 9 * level - 158
        base = (level * (9 * level - 325) + 4440) // 2

    progress = vitals.experience_progress
    if progress is None or not 0 <= progress <= 1:
        return ExperienceBalance(cost, None, None)
    product, = _FLOAT32.unpack(_FLOAT32.pack(progress * cost))
    earned = math.floor(product + 0.5)
    return ExperienceBalance(cost, earned, base + earned)


@dataclass(frozen=True, slots=True)
class PlayerEffect:
    """Last reported effect properties, including its server-announced duration."""

    id: int
    amplifier: int
    duration_ticks: int
    show_particles: bool
    ambient: bool


def _effect_from_packet(packet) -> PlayerEffect:
    return PlayerEffect(
        int(packet.effect_id), int(packet.effect_amplifier), int(packet.effect_duration_ticks),
        bool(packet.show_particles), bool(packet.ambient),
    )


@dataclass(frozen=True, slots=True)
class PlayerAbilities:
    """Effective server ability bits and declared speeds. None denotes an unknown value."""

    abilities_set: int = 0
    ability_values: int = 0
    may_fly: bool | None = None
    flying: bool | None = None
    fly_speed: float | None = None
    vertical_fly_speed: float | None = None
    walk_speed: float | None = None


@dataclass(frozen=True, slots=True)
class GlideBoostEvent:
    """Server-reported glide boost duration and last handled input tick."""

    runtime_id: int
    duration_ticks: int
    last_handled_input_tick: int


@dataclass(frozen=True, slots=True)
class DeathEvent:
    cause: str
    messages: tuple[str, ...]


@dataclass(slots=True)
class _RespawnCycle:
    ready: RawPacket | None = None
    request_sent: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[tuple[float, float, float]] | None = None


def movement_speed_attribute(packet) -> float | None:
    """Read the movement speed from a decoded actor attributes packet."""
    for attribute in packet.attribute_data:
        if str(attribute.name) == "minecraft:movement":
            return float(attribute.current_value)
    return None


def air_metadata(packet, binding):
    ids = (binding.ActorDataIDs.AIR_SUPPLY, binding.ActorDataIDs.AIR_SUPPLY_MAX)
    return {
        entry.id: int(entry.payload.value) for entry in packet.packed_items.data
        if entry.id in ids and isinstance(entry.payload, binding.DataItemShortPayload)
    }


def _low_actor_flag(packet, binding, flag) -> bool | None:
    for entry in packet.packed_items.data:
        if entry.id == 0 and isinstance(entry.payload, binding.DataItemInt64Payload):
            return bool(entry.payload.value & (1 << int(flag)))
    return None


def swimming_metadata(packet, binding) -> bool | None:
    """Read the swimming bit from the low actor flags, when present."""
    return _low_actor_flag(packet, binding, binding.ActorFlags.SWIMMING)


class PlayerState:
    def __init__(self, packets, runtime_id: int, *, unique_id: int | None = None,
                 auto_respawn: bool = False, initial_packets=(), replay_game_mode=None,
                 glide_boost_tick: Callable[[], int | None] | None = None):
        self.packets = packets
        self.runtime_id = int(runtime_id)
        self.unique_id = unique_id
        self.abilities = PlayerAbilities()
        self._flight_revision = 0
        self._flight_authorization_revision = 0
        self._last_resolved_game_mode = None
        self._flight_in_creative = False
        self._flight_explicitly_permitted = False
        self._observed_flying: bool | None = None
        self._submitted_flying: bool | None = None
        self.health: float | None = None
        self.absorption: float | None = None
        self._collision_size: tuple[float | None, float | None] = (None, None)
        self.hunger: float | None = None
        self.saturation: float | None = None
        self.air_supply: int | None = None
        self.air_supply_max: int | None = None
        self.experience_level: int | None = None
        self.experience_progress: float | None = None
        self._experience_observers: dict[object, Callable[[PlayerVitals], None]] = {}
        self._effects: dict[int, PlayerEffect] = {}
        self._glide_boost_tick = None
        self._glide_boost_timing: tuple[int, int] | None = None
        self._auto_respawn = False
        self._cycle: _RespawnCycle | None = None
        self._on_spawn: Callable[[Literal["initial", "respawn"]], None] | None = None
        self._awaiting_spawn_alive = False
        self._closed = False
        self._disconnected = False
        self._respawn_tasks: set[asyncio.Task] = set()
        self._respawn_wire = asyncio.Lock()
        for packet in initial_packets:
            if replay_game_mode is not None and not isinstance(packet, RawPacket):
                self._observe_game_mode(replay_game_mode(packet))
            else:
                self._on_packet(packet)
        self._glide_boost_tick = glide_boost_tick
        self._auto_respawn = auto_respawn
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        if self._auto_respawn and self._cycle is not None and self._cycle.ready is None:
            asyncio.get_running_loop().call_soon(self._auto_request, self._cycle)

    def _start_spawn_observation(self, callback: Callable[[Literal["initial", "respawn"]], None]) -> None:
        self._on_spawn = callback
        callback("initial")

    @property
    def is_dead(self) -> bool:
        return self.health is not None and self.health <= 0

    def vitals(self) -> PlayerVitals:
        return PlayerVitals(
            health=self.health, hunger=self.hunger, saturation=self.saturation,
            air_supply=self.air_supply, air_supply_max=self.air_supply_max,
            experience_level=self.experience_level, experience_progress=self.experience_progress,
        )

    def subscribe_experience(self, callback: Callable[[PlayerVitals], None]) -> Callable[[], None]:
        observers = self._experience_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def status_effects(self) -> tuple[PlayerEffect, ...]:
        return tuple(self._effects[effect_id] for effect_id in sorted(self._effects))

    def _gap_jump_allowed(self) -> bool:
        """Check observed player state for the calibrated ground jump."""
        return (not self._closed and not self._disconnected and not self.is_dead
                and self.abilities.flying is False and self.flight_target(require_permission=False)[0] is False
                and _JUMP_MOVEMENT_EFFECTS.isdisjoint(self._effects))

    def _local_respawn(self, raw):
        packet = self.packets.protocol.decode(raw)
        # Vanilla uses runtime ID 0 for the recipient in clientbound respawn packets.
        return packet if packet.runtime_id in (0, self.runtime_id) else None

    def _attributes(self, raw):
        packet = self.packets.protocol.decode(raw)
        return packet.attribute_data if int(packet.runtime_id) == self.runtime_id else ()

    def _read_attribute(self, raw, name: str) -> float | None:
        if raw.packet_id != self.packets.protocol.binding.UpdateAttributesPacket.ID:
            return None
        return next((float(attribute.current_value) for attribute in self._attributes(raw)
                     if str(attribute.name) == name), None)

    def _read_health(self, raw) -> float | None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.SetHealthPacket.ID:
            return float(self.packets.protocol.decode(raw).health)
        return self._read_attribute(raw, "minecraft:health")

    def _observe_collision_size(self, packet) -> None:
        b = self.packets.protocol.binding
        width, height = self._collision_size

        def positive(value):
            return float(value) if math.isfinite(value) and value > 0 else None

        for entry in packet.packed_items.data:
            payload = entry.payload
            if entry.id == 53:
                width = positive(payload.value) if isinstance(payload, b.DataItemFloatPayload) else None
            elif entry.id == 54:
                height = positive(payload.value) if isinstance(payload, b.DataItemFloatPayload) else None
            elif entry.id == b.ActorDataIDs.COLLISION_BOX:
                if isinstance(payload, b.DataItemVec3Payload):
                    vector = payload.value
                    width, height = positive(vector.x), positive(vector.y)
                else:
                    width = height = None
        self._collision_size = (width, height)

    def _replay_collision_size(self, movement_packets) -> None:
        protocol = self.packets.protocol
        if protocol.number != 2225:
            return
        b = protocol.binding
        # Startup position negotiation can include size-only changes.
        for raw in movement_packets:
            if raw.packet_id == b.SetActorDataPacket.ID:
                self._observe_collision_size(protocol.decode(raw))
            elif raw.packet_id == b.ChangeDimensionPacket.ID:
                self._collision_size = (None, None)

    def _own_abilities(self, raw):
        data = self.packets.protocol.decode(raw).data
        return data if int(data.target_player) == self.unique_id else None

    def _read_abilities(self, data) -> PlayerAbilities:
        defined = values = 0
        fly_speed = vertical_fly_speed = walk_speed = None
        index = self.packets.protocol.binding.AbilitiesIndex
        # Later, higher-priority layers override only the abilities they define.
        for layer in sorted(data.layers, key=lambda item: int(item.serialized_layer)):
            mask = int(layer.abilities_set)
            values = (values & ~mask) | (int(layer.ability_values) & mask)
            defined |= mask
            if mask & (1 << int(index.FLY_SPEED)):
                fly_speed = layer.fly_speed
            if mask & (1 << int(index.VERTICAL_FLY_SPEED)):
                # Older packet layouts omit the vertical speed field.
                vertical_fly_speed = getattr(layer, "vertical_fly_speed", None)
            if mask & (1 << int(index.WALK_SPEED)):
                walk_speed = layer.walk_speed

        def flag(index) -> bool | None:
            bit = 1 << int(index)
            return bool(values & bit) if defined & bit else None

        def finite_speed(value: float | None) -> float | None:
            return value if value is not None and math.isfinite(value) else None

        return PlayerAbilities(
            defined, values, flag(index.MAY_FLY), flag(index.FLYING),
            fly_speed=finite_speed(fly_speed),
            vertical_fly_speed=finite_speed(vertical_fly_speed),
            walk_speed=finite_speed(walk_speed),
        )

    def _on_packet(self, raw) -> None:
        if self._disconnected:
            return
        b = self.packets.protocol.binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self._disconnected = True
            self._glide_boost_timing = None
            for task in self._respawn_tasks:
                task.cancel()
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            self._collision_size = (None, None)
            self._glide_boost_timing = None
        elif raw.packet_id == b.ActorEventPacket.ID and self._on_spawn is not None:
            packet = self.packets.protocol.decode(raw)
            if packet.runtime_id == self.runtime_id:
                if packet.event_id == b.ActorEvent.DEATH:
                    self._awaiting_spawn_alive = True
                    self._glide_boost_timing = None
                elif packet.event_id == b.ActorEvent.SPAWN_ALIVE and self._awaiting_spawn_alive:
                    self._awaiting_spawn_alive = False
                    self._glide_boost_timing = None
                    self._on_spawn("respawn")
        elif raw.packet_id == b.RespawnPacket.ID:
            packet = self._local_respawn(raw)
            if packet is None:
                return
            if packet.state == b.PlayerRespawnState.SEARCHING_FOR_SPAWN:
                self._glide_boost_timing = None
                if self._cycle is None or self._cycle.ready is not None:
                    self._collision_size = (None, None)
                    self._cycle = _RespawnCycle()
                    if self._auto_respawn:
                        asyncio.get_running_loop().call_soon(self._auto_request, self._cycle)
            elif packet.state == b.PlayerRespawnState.READY_TO_SPAWN:
                self._glide_boost_timing = None
                if self._cycle is not None and self._cycle.ready is None:
                    self._cycle.ready = raw
        elif raw.packet_id == b.SetHealthPacket.ID:
            self.health = self._read_health(raw)
        elif raw.packet_id == b.UpdateAbilitiesPacket.ID:
            data = self._own_abilities(raw)
            if data is not None:
                self.abilities = self._read_abilities(data)
                explicitly_permitted = self.abilities.may_fly is True
                revoked = self._flight_explicitly_permitted and not explicitly_permitted
                self._flight_explicitly_permitted = explicitly_permitted
                self._observed_flying = self.abilities.flying
                if revoked:
                    self._invalidate_flight_attempts()
                elif self._observed_flying is not None or not self._can_attempt_flight():
                    self._reset_flight_target()
        elif raw.packet_id == b.UpdateAttributesPacket.ID:
            reported_experience = False
            for attribute in self._attributes(raw):
                field_name = VITAL_ATTRIBUTES.get(attribute.name)
                if field_name is None:
                    continue
                reported_experience = reported_experience or field_name in ("experience_level", "experience_progress")
                value = attribute.current_value
                if field_name != "experience_level":
                    setattr(self, field_name, float(value))
                elif getattr(self.packets.protocol, "number", None) != 2225:
                    self.experience_level = int(value)
                elif 0 <= value <= _MAX_EXPERIENCE_LEVEL_2225 and value == math.floor(value):
                    self.experience_level = int(value)
                else:
                    self.experience_level = None
            if reported_experience and self._experience_observers:
                snapshot = self.vitals()
                for token, callback in tuple(self._experience_observers.items()):
                    if token in self._experience_observers:
                        self.packets._notify((callback,), snapshot)
        elif raw.packet_id == b.SetActorDataPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.id == self.runtime_id:
                air = air_metadata(packet, b)
                self.air_supply = air.get(b.ActorDataIDs.AIR_SUPPLY, self.air_supply)
                self.air_supply_max = air.get(b.ActorDataIDs.AIR_SUPPLY_MAX, self.air_supply_max)
                self._observe_collision_size(packet)
                if (self._glide_boost_timing is not None
                        and _low_actor_flag(packet, b, b.ActorFlags.GLIDING) is False):
                    _, received_tick = self._glide_boost_timing
                    self._glide_boost_timing = (0, received_tick)
        elif self._glide_boost_tick is not None and raw.packet_id == b.MovementEffectPacket.ID:
            if self._awaiting_spawn_alive or (self._cycle is not None and self._cycle.ready is None):
                return
            tick = self._glide_boost_tick()
            if tick is not None and (boost := self._glide_boost_event(raw)) is not None:
                duration = boost.duration_ticks
                self._glide_boost_timing = (duration, tick) if duration >= 0 else None
        elif raw.packet_id == b.MobEffectPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.runtime_id == self.runtime_id:
                events = b.MobEffectPacket.Event
                if packet.event_id in (events.ADD, events.UPDATE):
                    effect = _effect_from_packet(packet)
                    self._effects[effect.id] = effect
                elif packet.event_id == events.REMOVE:
                    self._effects.pop(int(packet.effect_id), None)

    async def wait_for_health_update(self, timeout: float = 15) -> float:
        """Return the next server health update, not a locally predicted value."""
        b = self.packets.protocol.binding
        raw = await self._wait(
            (b.UpdateAttributesPacket.ID, b.SetHealthPacket.ID), timeout,
            lambda packet: self._read_health(packet) is not None, require_live_after_match=False,
        )
        return self._read_health(raw)

    async def wait_for_hunger_update(self, timeout: float = 15) -> float:
        """Return the next server-provided local-player hunger value."""
        def hunger(packet):
            return self._read_attribute(packet, "minecraft:player.hunger")

        raw = await self._wait(
            self.packets.protocol.binding.UpdateAttributesPacket.ID, timeout,
            lambda packet: hunger(packet) is not None, require_live_after_match=False,
        )
        return hunger(raw)

    def _check_connection(self) -> None:
        if self._closed or self._disconnected:
            raise ConnectionError("player connection closed during lifecycle operation")

    def _reset_flight_target(self) -> None:
        self._flight_revision += 1
        self._submitted_flying = None

    def _invalidate_flight_attempts(self) -> None:
        self._flight_authorization_revision += 1
        self._reset_flight_target()

    def _observe_game_mode(self, mode) -> None:
        game_type = self.packets.protocol.binding.GameType
        if not isinstance(mode, game_type):
            # An unresolved mode only removes eligibility based on creative mode.
            if self._flight_in_creative and not self._flight_explicitly_permitted:
                self._invalidate_flight_attempts()
            self._flight_in_creative = False
            return
        if self._last_resolved_game_mode is not None and mode != self._last_resolved_game_mode:
            # The mode change makes recorded flight bits stale for operations.
            self._flight_explicitly_permitted = False
            self._observed_flying = None
            self._invalidate_flight_attempts()
        self._last_resolved_game_mode = mode
        self._flight_in_creative = mode == game_type.CREATIVE

    def _can_attempt_flight(self) -> bool:
        return self._flight_in_creative or self._flight_explicitly_permitted

    @property
    def flight_authorization_revision(self) -> int:
        return self._flight_authorization_revision

    def flight_target(self, *, require_permission: bool) -> tuple[bool | None, int]:
        self._check_connection()
        if require_permission and not self._can_attempt_flight():
            raise PermissionError("server has not granted flight")
        target = self._observed_flying if self._submitted_flying is None else self._submitted_flying
        if target is None and not require_permission:
            target = False
        return target, self._flight_revision

    def flight_submitted(self, flying: bool, revision: int) -> None:
        # A newer flight observation can arrive while the final send is suspended.
        if revision == self._flight_revision:
            self._submitted_flying = flying

    async def _wait(self, packet_id: int | tuple[int, ...], timeout, match=None, *, require_live_after_match=True):
        self._check_connection()
        b = self.packets.protocol.binding
        packet_ids = (packet_id,) if isinstance(packet_id, int) else packet_id
        raw = await self.packets.wait_for_packet(
            (*packet_ids, b.DisconnectPacket.ID), timeout=timeout,
            match=lambda raw: raw.packet_id == b.DisconnectPacket.ID or match is None or match(raw),
        )
        if require_live_after_match or raw.packet_id == b.DisconnectPacket.ID:
            self._check_connection()
        return raw

    async def _wait_for_change(self, packet_ids, snapshot, timeout):
        before = update = snapshot()

        def changed(_raw):
            nonlocal update
            update = snapshot()
            return update != before

        # A later packet in the same batch can disconnect after this observation.
        await self._wait(packet_ids, timeout, changed, require_live_after_match=False)
        return update

    async def wait_for_vitals_update(self, timeout: float) -> PlayerVitals:
        b = self.packets.protocol.binding
        return await self._wait_for_change(
            (b.SetHealthPacket.ID, b.UpdateAttributesPacket.ID, b.SetActorDataPacket.ID), self.vitals, timeout,
        )

    async def wait_for_status_effects_update(self, timeout: float) -> tuple[PlayerEffect, ...]:
        return await self._wait_for_change(self.packets.protocol.binding.MobEffectPacket.ID,
                                           self.status_effects, timeout)

    async def wait_for_abilities(self, timeout: float) -> PlayerAbilities:
        snapshot = None

        def own_update(raw):
            nonlocal snapshot
            data = self._own_abilities(raw)
            if data is None:
                return False
            snapshot = self._read_abilities(data)
            return True

        await self._wait(self.packets.protocol.binding.UpdateAbilitiesPacket.ID, timeout, own_update,
                         require_live_after_match=False)
        assert snapshot is not None
        return snapshot

    @property
    def estimated_glide_boost_ticks_remaining(self) -> int | None:
        if self._glide_boost_timing is None or self._glide_boost_tick is None:
            return None
        tick = self._glide_boost_tick()
        if tick is None:
            return None
        duration, received_tick = self._glide_boost_timing
        return max(0, duration - (tick - received_tick))

    def _glide_boost_event(self, raw) -> GlideBoostEvent | None:
        protocol = self.packets.protocol
        packet = protocol.decode(raw)
        if (int(packet.runtime_id) == self.runtime_id
                and packet.effect_type == protocol.binding.MovementEffectType.GLIDE_BOOST):
            return GlideBoostEvent(self.runtime_id, int(packet.effect_duration), int(packet.tick))
        return None

    async def wait_for_glide_boost(self, timeout: float | None = 15) -> GlideBoostEvent:
        event = None

        def own_boost(raw):
            nonlocal event
            event = self._glide_boost_event(raw)
            return event is not None

        await self._wait(self.packets.protocol.binding.MovementEffectPacket.ID, timeout, own_boost,
                         require_live_after_match=False)
        assert event is not None
        return event

    async def wait_for_death(self, timeout: float = 15) -> DeathEvent:
        protocol = self.packets.protocol
        raw = await self._wait(protocol.binding.DeathInfoPacket.ID, timeout)
        message = protocol.decode(raw).death_cause_message
        return DeathEvent(message.attack_name, tuple(message.message_list))

    async def wait_for_respawn(self, timeout: float = 15) -> tuple[float, float, float]:
        protocol = self.packets.protocol
        raw = await self._wait(protocol.binding.RespawnPacket.ID, timeout,
                               lambda raw: self._cycle is not None and raw is self._cycle.ready)
        pos = protocol.decode(raw).pos
        return float(pos.x), float(pos.y), float(pos.z)

    def _auto_request(self, cycle: _RespawnCycle) -> None:
        if not self._closed and not self._disconnected and cycle is self._cycle and cycle.ready is None:
            self._start_respawn(cycle)

    def _start_respawn(self, cycle: _RespawnCycle) -> None:
        if cycle.task is None or (cycle.task.done() and not cycle.request_sent.is_set() and cycle.ready is None):
            cycle.task = asyncio.create_task(self._handshake(cycle))
            self._respawn_tasks.add(cycle.task)
            cycle.task.add_done_callback(self._respawn_finished)

    def _respawn_finished(self, task):
        self._respawn_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if self._auto_respawn and error is not None and not isinstance(error, ConnectionError):
                task.get_loop().call_exception_handler({"message": "automatic player respawn failed", "exception": error})

    async def _handshake(self, cycle: _RespawnCycle) -> tuple[float, float, float]:
        protocol = self.packets.protocol
        b = protocol.binding
        # A later search can arrive while the previous acknowledgment is still being sent.
        async with self._respawn_wire:
            pending = asyncio.create_task(self._wait(b.RespawnPacket.ID, None, lambda raw: raw is cycle.ready))
            try:
                await asyncio.sleep(0)
                self._check_connection()
                if cycle.ready is not None:
                    raise RuntimeError("server has already ended the respawn search")
                await self.packets.send(b.RespawnPacket(pos=b.Vec3(x=0.0, y=0.0, z=0.0),
                                                       state=b.PlayerRespawnState.CLIENT_READY_TO_SPAWN,
                                                       runtime_id=self.runtime_id))
                cycle.request_sent.set()
                pos = protocol.decode(await pending).pos
                self._check_connection()
                zero = b.BlockPos(x=0, y=0, z=0)
                await self.packets.send(b.PlayerActionPacket(runtime_id=self.runtime_id,
                                                             action=b.PlayerActionType.RESPAWN,
                                                             pos=zero, result_pos=zero, face=0))
                return float(pos.x), float(pos.y), float(pos.z)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _request_respawn(self, timeout) -> _RespawnCycle:
        b = self.packets.protocol.binding
        self._check_connection()
        cycle = self._cycle
        if cycle is None or (cycle.ready is not None and (cycle.task is None or cycle.task.done())):
            def searching(raw):
                nonlocal cycle
                packet = self._local_respawn(raw)
                if packet is not None and packet.state == b.PlayerRespawnState.SEARCHING_FOR_SPAWN:
                    cycle = self._cycle
                    return True
                return False

            await self._wait(b.RespawnPacket.ID, timeout, searching)
        assert cycle is not None
        self._start_respawn(cycle)
        return cycle

    async def submit_respawn(self, *, timeout: float = 15) -> None:
        """Submit the respawn input and let the session acknowledge the server's reply."""
        async with asyncio.timeout(timeout):
            cycle = await self._request_respawn(timeout)
            task = cycle.task
            sent = asyncio.create_task(cycle.request_sent.wait())
            try:
                done, _ = await asyncio.wait((task, sent), return_when=asyncio.FIRST_COMPLETED)
                if task in done and not cycle.request_sent.is_set():
                    self._check_connection()
                    await task
            finally:
                sent.cancel()
                await asyncio.gather(sent, return_exceptions=True)

    async def respawn(self, *, timeout: float = 15) -> tuple[float, float, float]:
        """Return the server's READY_TO_SPAWN position after sending its acknowledgment."""
        async with asyncio.timeout(timeout):
            cycle = await self._request_respawn(timeout)
            try:
                return await asyncio.shield(cycle.task)
            except asyncio.CancelledError:
                if not asyncio.current_task().cancelling():
                    self._check_connection()
                raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._glide_boost_timing = None
            self._on_spawn = None
            self._end_experience_observation()
            self._unsubscribe()
            for task in self._respawn_tasks:
                task.cancel()

    async def aclose(self) -> None:
        self.close()
        if self._respawn_tasks:
            await asyncio.gather(*self._respawn_tasks, return_exceptions=True)

    def _end_experience_observation(self) -> None:
        self._experience_observers.clear()
