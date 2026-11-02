from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, fields, replace

import bedrock_protocol as bp
from bedrock_protocol.wire import BatchCodec, Compression

from . import _inventory, transport
from . import events as ev
from ._projection import unwrap
from .auth import build_login, create_identity, derive_session_key
from .observations import ActorSnapshot, ContainerSnapshot, InventorySnapshot, SlotObservation, Statistics
from .operations import ItemCancellation, ItemOperation, ItemOperationPhase, ItemOperationResult, ItemRequestOutcome
from .types import (
    BotState,
    BotStatus,
    Input,
    InventoryArea,
    InventorySlot,
    ItemCount,
    ItemRequestKey,
    ServerDisconnectReason,
    SlotState,
    TradeWindowObservation,
    Vec2,
    Vec3,
    WindowRef,
)

REQUIRED_IDS = frozenset(
    (
        2,
        3,
        5,
        6,
        7,
        11,
        12,
        13,
        14,
        15,
        17,
        18,
        19,
        27,
        29,
        30,
        36,
        39,
        42,
        45,
        46,
        47,
        49,
        50,
        58,
        61,
        70,
        80,
        111,
        115,
        143,
        148,
        161,
        162,
        174,
    )
)


class _MalformedPacket(RuntimeError):
    pass


def bounded_ints(values, low, high, limit):
    out = []
    for value in values:
        if type(value) is not int or not low <= value <= high:
            raise TypeError("integer value outside protocol range")
        if len(out) >= limit:
            raise ValueError("outbound queue limit exceeded")
        out.append(value)
    return out


def vec(value):
    return Vec3(value.x, value.y, value.z)


def finite(*values):
    for value in values:
        value_type = type(value)
        if value_type is Vec3:
            if not (math.isfinite(value.x) and math.isfinite(value.y) and math.isfinite(value.z)):
                return False
        elif value_type is Vec2:
            if not (math.isfinite(value.x) and math.isfinite(value.y)):
                return False
        elif isinstance(value, (Vec3, Vec2)):
            if not all(math.isfinite(getattr(value, f.name)) for f in fields(value)):
                return False
        elif not math.isfinite(value):
            return False
    return True


def owned(value):
    return None if value is None else value.copy()


@dataclass(slots=True)
class _Command:
    token: bp.EncodedPacket
    size: int
    play: bool
    operation: object = None
    valid: object = None


@dataclass(slots=True)
class _Boundary:
    future: asyncio.Future
    change: object = None


@dataclass(frozen=True, slots=True)
class _InitialPosition:
    runtime_id: int
    position: Vec3
    teleport: bool
    acknowledge: bool


@dataclass(frozen=True, slots=True)
class _InitialHealth:
    runtime_id: int | None
    value: float


class Session:
    def __init__(self, bot, generation, ready):
        self.bot = bot
        self.runtime = bot._runtime
        self.options = bot._options
        self.id = generation
        self.ready = ready
        self.p = getattr(bp, f"v{self.options.protocol}")
        self.codec = bp.Codec(self.options.protocol)
        if REQUIRED_IDS - set(self.codec.supported_packet_ids):
            raise RuntimeError("required protocol packet bindings are unavailable")
        self.batch = BatchCodec(self.codec)
        self.identity = None
        self.status = BotStatus(session_id=generation)
        self.stats = {f.name: 0 for f in fields(Statistics)
                      if not f.name.startswith(("connected_bytes_", "packet_counts_", "play_packet_counts_"))}
        self.packet_counts_in = [0] * 1024
        self.packet_counts_out = [0] * 1024
        self.play_packet_counts_in = [0] * 1024
        self.play_packet_counts_out = [0] * 1024
        self.connection = None
        self.active = True
        self.finished = False
        self.started = self.cleaning = False
        self.error = None
        self.reason = ""
        self.disconnect_reason = None
        self.settings = self.handshake = self.login = self.start_game = self.spawn = self.initialized = False
        self.initial_self_state = deque()
        self.registry_observed = False
        self.registry = ()
        self.actors = {}
        self.unique_ids = {}
        self.inventories = {}
        self.container = None
        self.window = None
        self.window_generation = 0
        self.offer_revision = 0
        self.interacting = None
        self.input = None
        self.look = None
        self.input_epoch = 0
        self.equipment_epoch = 0
        self.handled_teleport = False
        self.dimension_deadline = None
        self.loading_screen_id = None
        self.respawn_phase = 0
        self.probe = None
        self.last_probe_stamp = 0
        self.pending = None
        self.item_changes = ()
        self.item_authority = set()
        self.next_request_id = -1
        self.operation_deadline = None
        self.queue = deque()
        self.queue_bytes = 0
        self.queue_packets = 0
        self.send_event = asyncio.Event()
        self.next_tick = None

    def usable(self):
        return self.active and self.id == self.bot._generation and not self.runtime._stopping

    def emit(self, kind, factory):
        if self.usable():
            self.runtime._emit(self.bot, kind, factory)

    def state(self, state, reason=""):
        self.status = replace(self.status, state=state)
        self.emit(ev.EventKind.state, lambda: ev.StateEvent(state, reason, self.disconnect_reason))

    def lifecycle_state(self, state):
        self.status = replace(self.status, state=state)
        try:
            self.runtime._emit(
                self.bot, ev.EventKind.state, lambda: ev.StateEvent(state, self.reason, self.disconnect_reason)
            )
        except BaseException as error:
            self.runtime._background_failure(error)

    def invalidate(self, reason):
        self.active = False
        self.reason = reason
        self.input = self.look = None
        self.input_epoch += 1
        self.equipment_epoch += 1
        self.send_event.set()
        if self.pending is not None:
            self.invalidate_operation(reason)

    def fail(self, error):
        self.error = error
        self.invalidate(str(error))
        if not self.ready.done():
            self.ready.set_exception(error)

    async def run(self):
        self.started = True
        tasks = []
        try:
            self.state(BotState.scheduled)
            if self.options.start_delay_ms and self.id == 1:
                await asyncio.sleep(self.options.start_delay_ms / 1000)
            if not self.usable():
                return
            self.state(BotState.connecting)
            if not self.usable():
                return
            self.identity = create_identity(self.options.name, self.options.uuid, xuid=self.options.xuid)
            async with asyncio.timeout(self.options.connect_timeout_ms / 1000):
                self.connection = await transport._open_connection(self.options, self.identity)
            if not self.usable():
                return
            self.spawn_deadline = self.runtime._loop.time() + self.options.spawn_timeout_ms / 1000
            self.state(BotState.negotiating)
            if not self.usable():
                return
            self.enqueue(self.p.RequestNetworkSettingsPacket(client_network_version=self.options.protocol))
            tasks = [
                asyncio.create_task(self._work(fn), name=name)
                for fn, name in (
                    (self.reader, "bedrock-reader"),
                    (self.sender, "bedrock-sender"),
                    (self.ticker, "bedrock-ticker"),
                )
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError as error:
            if not asyncio.current_task().cancelling() and self.error is None:
                self.fail(error)
                self.runtime._background_failure(error)
        except BaseException as exc:
            if self.runtime._suppressed and self.error is not exc:
                self.runtime._background_failure(exc)
            self.fail(exc)
        finally:
            self.cleaning = True
            self.invalidate(self.reason or "session closed")
            self.lifecycle_state(BotState.closing)
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for error in results:
                if (
                    isinstance(error, BaseException)
                    and not isinstance(error, asyncio.CancelledError)
                    and self.error is None
                ):
                    if self.runtime._suppressed:
                        self.runtime._background_failure(error)
                    self.error = error
            for command in self.queue:
                if isinstance(command, _Boundary) and not command.future.done():
                    command.future.cancel()
            self.queue.clear()
            self.initial_self_state.clear()
            self.queue_bytes = self.queue_packets = 0
            self.actors.clear()
            self.unique_ids.clear()
            self.inventories.clear()
            self.container = self.window = None
            if self.connection is not None:
                try:
                    await self.connection.close()
                except BaseException as exc:
                    self.error = exc
                    self.runtime._background_failure(exc)
            self.lifecycle_state(BotState.failed if self.error else BotState.closed)
            if not self.ready.done():
                self.ready.set_exception(self.error or RuntimeError("session closed before Ready"))
            self.finished = True
            self.bot._wake.set()

    async def _work(self, function):
        try:
            await function()
        except asyncio.CancelledError as error:
            if not asyncio.current_task().cancelling():
                self.fail(error)
                self.runtime._background_failure(error)
            raise
        except BaseException as error:
            self.fail(error)
            if self.runtime._suppressed:
                self.runtime._background_failure(error)
            raise

    def enqueue(self, packet, *, operation=None, valid=None, fail_session=True):
        if not self.usable():
            raise RuntimeError("session is closing")
        try:
            token = self.codec.prepare_packet(packet)
            size = len(token) + 5
        except BaseException as error:
            if fail_session:
                self.fail(error)
            raise
        if self.queue_packets >= transport.COMMAND_PACKETS or size > transport.COMMAND_BYTES - self.queue_bytes:
            error = RuntimeError("outbound queue limit exceeded")
            if fail_session:
                self.fail(error)
            raise error
        self.queue.append(_Command(token, size, self.initialized, operation, valid))
        self.queue_bytes += size
        self.queue_packets += 1
        self.send_event.set()

    async def flush(self, change=None):
        future = self.runtime._loop.create_future()
        self.queue.append(_Boundary(future, change))
        self.send_event.set()
        await asyncio.shield(future)

    def command_valid(self, command):
        return (command.operation is None or command.operation is self.pending) and (
            command.valid is None or command.valid()
        )

    async def sender(self):
        while self.usable():
            await self.send_event.wait()
            self.send_event.clear()
            while self.queue and self.usable():
                first = self.queue[0]
                if isinstance(first, _Boundary):
                    self.queue.popleft()
                    if first.change:
                        first.change()
                    if not first.future.done():
                        first.future.set_result(None)
                    continue
                commands = []
                for command in self.queue:
                    if isinstance(command, _Boundary):
                        break
                    commands.append(command)
                valid = [command for command in commands if self.command_valid(command)]
                if valid:
                    wire = self.batch.encode_prepared([command.token for command in valid])
                    if len(wire) > transport.MAX_WIRE_MESSAGE:
                        raise ValueError("outbound wire message limit exceeded")
                    for command in valid:
                        if command.operation is not None:
                            self.item_authority.clear()
                            command.operation._submit()
                            self.operation_deadline = (
                                self.runtime._loop.time() + self.options.item_request_timeout_ms / 1000
                            )
                    await self.connection.send(wire)
                    self.stats["batch_bytes_out"] += len(wire)
                    self.stats["packets_out"] += len(valid)
                    for command in valid:
                        self.packet_counts_out[command.token.packet_id] += 1
                        if command.play:
                            self.play_packet_counts_out[command.token.packet_id] += 1
                for command in commands:
                    assert self.queue.popleft() is command
                    self.queue_bytes -= command.size
                    self.queue_packets -= 1

    async def reader(self):
        while self.usable():
            data = await self.connection.recv()
            self.stats["batch_bytes_in"] += len(data)
            if len(data) > transport.MAX_WIRE_MESSAGE:
                raise ValueError("inbound wire message limit exceeded")
            if not self.usable():
                return
            try:
                batch = self.batch.decode(
                    data,
                    packet_ids=None if self.options.validate_all_packets else REQUIRED_IDS,
                    recover_packet_errors=self.options.recover_packet_errors,
                )
            except bp.ProtocolError as error:
                packet_id = getattr(error, "packet_id", None)
                self.decode_error(-1 if packet_id is None else packet_id, str(error), recoverable=False)
                raise
            self.stats["packets_in"] += batch.packet_count
            headers = batch.headers
            for header in headers:
                self.packet_counts_in[header.packet_id] += 1
            decoded_packets = iter(batch)
            decode_errors = batch.errors
            errors = {error.packet_index: error for error in decode_errors} if decode_errors else None
            for packet_index, header in enumerate(headers):
                if not self.usable():
                    return
                if errors is not None and packet_index in errors:
                    error = errors[packet_index]
                    self.decode_error(header.packet_id, error.reason, recoverable=True)
                    continue
                if not self.options.validate_all_packets and header.packet_id not in REQUIRED_IDS:
                    continue
                decoded = next(decoded_packets)
                if self.initialized:
                    self.play_packet_counts_in[decoded.header.packet_id] += 1
                self.emit(ev.EventKind.packet_decoded, lambda: ev.PacketDecodedEvent(decoded))
                if not self.usable():
                    return
                if decoded.header.sender_subclient_id or decoded.header.target_subclient_id:
                    continue
                try:
                    if (
                        self.options.transport == "raknet"
                        and decoded.header.packet_id == 3
                        and packet_index != len(headers) - 1
                    ):
                        raise _MalformedPacket("server handshake must be the final packet in a RakNet batch")
                    await self.handle(decoded.payload)
                except _MalformedPacket as error:
                    self.decode_error(decoded.header.packet_id, str(error), recoverable=False)
                    raise

    def decode_error(self, packet_id, reason, *, recoverable):
        self.stats["decode_errors"] += 1
        self.emit(ev.EventKind.decode_error, lambda: ev.DecodeErrorEvent(packet_id, reason, recoverable))

    async def handle(self, packet):
        p = self.p
        id = packet.packet_id
        pinned_raknet = self.options.transport == "raknet" and bool(self.options.server_key_sha256)
        if pinned_raknet and not self.login and id not in (143, 3, 2, 5):
            raise _MalformedPacket("gameplay packet received before pinned login success")
        if id == 143:
            if self.settings or self.status.state != BotState.negotiating:
                raise RuntimeError("unexpected NetworkSettings")
            await self.flush(
                lambda: self.batch.set_compression(
                    Compression(int(packet.compression_algorithm)), packet.compression_threshold
                )
            )
            self.settings = True
            login = build_login(self.identity, self.options)
            self.state(BotState.logging_in)
            if self.usable():
                self.enqueue(p.LoginPacket(client_network_version=self.options.protocol, connection_request=login))
        elif id == 3:
            if not self.settings or self.handshake or self.login:
                raise RuntimeError("unexpected server handshake")
            try:
                if self.options.transport == "raknet":
                    key = derive_session_key(
                        self.identity, packet.token, server_key_sha256=self.options.server_key_sha256
                    )
                else:
                    # NetherNet protects the stream; retain its existing transport-level identity pin.
                    derive_session_key(self.identity, packet.token)
            except ValueError:
                raise _MalformedPacket("invalid server handshake") from None
            if self.options.transport == "raknet":
                await self.flush(lambda: self.batch.enable_encryption(key))
            else:
                await self.flush()
            self.handshake = True
            self.enqueue(p.ClientToServerHandshakePacket())
        elif id == 2:
            if packet.status == p.PlayStatus.LOGIN_SUCCESS:
                if not self.settings:
                    raise RuntimeError("LoginSuccess before settings")
                if self.options.transport == "raknet" and self.options.server_key_sha256 and not self.handshake:
                    raise RuntimeError("LoginSuccess before pinned server handshake")
                if not self.login:
                    self.login = True
                    self.enqueue(p.ClientCacheStatusPacket(enabled=self.options.client_cache))
                    self.state(BotState.spawning)
                    await self.try_ready()
            elif packet.status == p.PlayStatus.PLAYER_SPAWN:
                if pinned_raknet and not self.login:
                    raise _MalformedPacket("PlayerSpawn before pinned LoginSuccess")
                self.spawn = True
                await self.try_ready()
            else:
                raise RuntimeError("server rejected login")
        elif id in (6, 7):
            if self.options.protocol == 2193:
                cls = p.ResourcePackClientResponsePacket
                response = bp.Variant(
                    3 if id == 7 else 2, cls.ResourcePackStackFinished() if id == 7 else cls.DownloadingFinished()
                )
                self.enqueue(cls(response=response))
            else:
                response = (
                    p.ResourcePackResponse.RESOURCE_PACK_STACK_FINISHED
                    if id == 7
                    else p.ResourcePackResponse.DOWNLOADING_FINISHED
                )
                self.enqueue(p.ResourcePackClientResponsePacket(response=response, downloading_packs=[]))
        elif id == 11:
            position = vec(packet.pos)
            if not finite(position):
                raise _MalformedPacket("non-finite StartGame position")
            self.status = replace(
                self.status,
                actor_runtime_id=packet.runtime_id,
                position=position,
                dimension=packet.settings.spawn_settings.dimension,
            )
            self.input = self.look = None
            self.input_epoch += 1
            self.start_game = True
            self.apply_initial_self_state()
            await self.try_ready()
        elif id == 162:
            self.registry = packet.items.copy()
            if not self.registry_observed:
                self.registry_observed = True
                self.enqueue(p.RequestChunkRadiusPacket(chunk_radius=16, max_chunk_radius=16))
            self.emit(ev.EventKind.item_registry, lambda: ev.ItemRegistryEvent(self.registry))
        elif id == 70:
            self.status = replace(self.status, chunk_radius=packet.chunk_radius)
            await self.try_ready()
        elif id == 5:
            messages = packet.messages.value
            self.disconnect_reason = ServerDisconnectReason(
                int(packet.reason),
                None if messages is None else messages.message,
                None if messages is None else messages.filtered_message,
            )
            reason = next(
                (text for text in (self.disconnect_reason.message, self.disconnect_reason.filtered_message)
                 if text and text.strip()),
                f"server Disconnect packet (reason {self.disconnect_reason.reason_code})",
            )
            raise RuntimeError(reason)
        else:
            self.handle_state(packet)

    async def try_ready(self):
        if (
            not self.usable()
            or self.initialized
            or not self.login
            or (self.options.transport == "raknet" and self.options.server_key_sha256 and not self.handshake)
            or not self.start_game
            or not self.spawn
            or self.status.chunk_radius <= 0
        ):
            return
        p = self.p
        if self.options.load_screen:
            for mode in (
                p.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN,
                p.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN,
            ):
                self.enqueue(p.ServerboundLoadingScreenPacket(loading_screen_packet_type=mode, loading_screen_id=None))
        self.enqueue(p.SetLocalPlayerAsInitializedPacket(player_id=self.status.actor_runtime_id))
        await self.flush()
        if not self.usable():
            return
        self.initialized = True
        if self.options.chunk_radius != 16:
            self.enqueue(
                p.RequestChunkRadiusPacket(
                    chunk_radius=self.options.chunk_radius, max_chunk_radius=self.options.chunk_radius
                )
            )
        await self.flush()
        if self.usable():
            self.next_tick = self.runtime._loop.time() + 0.05
            self.state(BotState.ready)
            if self.usable() and not self.ready.done():
                self.ready.set_result(None)

    def handle_state(self, packet):
        p = self.p
        id = packet.packet_id
        if id == 42:
            if not self.start_game:
                self.defer_self_state(_InitialHealth(None, float(packet.health)))
            else:
                self.health(float(packet.health))
        elif id == 58:
            self.stats["column_packets"] += 1
            if self.runtime._wants(self.bot, ev.EventKind.chunk):
                limit = packet.client_request_sub_chunk_limit
                if self.options.protocol == 1001 and packet.sub_chunks_count == 0xFFFFFFFF:
                    limit = -1
                blobs = (
                    tuple(entry.blob_id for entry in packet.cache_metadata) if packet.cache_metadata is not None else ()
                )
                self.emit(
                    ev.EventKind.chunk,
                    lambda: ev.ChunkEvent(
                        ev.ChunkEvent.Kind.column,
                        packet.dimension_id,
                        packet.pos.x,
                        packet.pos.z,
                        subchunk_limit=limit,
                        blob_ids=blobs,
                    ),
                )
        elif id == 174:
            entries = packet.sub_chunk_data
            if self.options.protocol == 1001 and not packet.cache_enabled:
                entries = packet.uncached_sub_chunk_data
            self.stats["subchunks"] += len(entries)
            if self.runtime._wants(self.bot, ev.EventKind.chunk):
                blobs = tuple(entry.blob_id for entry in entries if getattr(entry, "blob_id", None) is not None)
                self.emit(
                    ev.EventKind.chunk,
                    lambda: ev.ChunkEvent(
                        ev.ChunkEvent.Kind.subchunks,
                        packet.dimension_type,
                        packet.center_pos.x,
                        packet.center_pos.z,
                        entries=len(entries),
                        blob_ids=blobs,
                    ),
                )
        elif id == 29:
            if not self.start_game:
                for attribute in packet.attribute_data:
                    if attribute.name == "minecraft:health":
                        self.defer_self_state(_InitialHealth(packet.runtime_id, attribute.current_value))
            if self.start_game and packet.runtime_id == self.status.actor_runtime_id:
                for attribute in packet.attribute_data:
                    if attribute.name == "minecraft:health":
                        self.health(attribute.current_value)
                    if not self.usable():
                        return
            else:
                actor = self.actors.get(packet.runtime_id)
                if actor is None or not packet.attribute_data:
                    return
                attributes = list(actor.attributes)
                for attribute in packet.attribute_data:
                    value = p.SyncedAttribute(
                        name=attribute.name,
                        min_value=attribute.min_value,
                        current_value=attribute.current_value,
                        max_value=attribute.max_value,
                    )
                    for index, old in enumerate(attributes):
                        if old.name == attribute.name:
                            attributes[index] = value
                            break
                    else:
                        attributes.append(value)
                actor = replace(actor, attributes=tuple(attributes))
                self.actors[packet.runtime_id] = actor
                self.emit(ev.EventKind.actor, lambda: ev.ActorEvent(ev.ActorEvent.Kind.metadata, actor))
        elif id == 19:
            if not self.start_game:
                mode = packet.reset_position
                self.defer_self_state(_InitialPosition(
                    packet.player_id, vec(packet.pos),
                    mode == p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                    mode in (p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                             p.PlayerPositionModeComponent.PositionMode.RESPAWN),
                ))
            if self.start_game and packet.player_id == self.status.actor_runtime_id:
                mode = packet.reset_position
                self.position(
                    vec(packet.pos),
                    mode == p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                    False,
                    mode
                    in (
                        p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                        p.PlayerPositionModeComponent.PositionMode.RESPAWN,
                    ),
                )
            else:
                actor = self.actors.get(packet.player_id)
                if actor is not None and actor.type == "minecraft:player":
                    self.move_actor(actor, vec(packet.pos), packet.rot.x, packet.rot.y, packet.y_head_rot)
        elif id == 161:
            if packet.prediction_type == p.RewindType.PLAYER:
                self.position(vec(packet.pos), False, True, False)
        elif id == 13:
            actor = ActorSnapshot(
                packet.runtime_id,
                packet.entity_id,
                packet.actor_type,
                vec(packet.pos),
                vec(packet.velocity),
                packet.rot.x,
                packet.rot.y,
                packet.y_head_rotation,
                packet.y_body_rotation,
                packet.data.copy(),
                packet.synched_properties.copy(),
                packet.attributes.copy(),
                packet.links.copy(),
            )
            self.add_actor(actor)
        elif id == 15:
            self.add_actor(
                ActorSnapshot(
                    packet.runtime_id,
                    packet.id,
                    "minecraft:item",
                    vec(packet.pos),
                    vec(packet.velocity),
                    metadata=packet.data.copy(),
                    item=packet.item.copy(),
                    from_fishing=packet.is_from_fishing,
                )
            )
        elif id == 12:
            actor = ActorSnapshot(
                packet.runtime_id,
                packet.abilities_data.target_player,
                "minecraft:player",
                vec(packet.pos),
                velocity=vec(packet.velocity),
                pitch=packet.rot.x,
                yaw=packet.rot.y,
                head_yaw=packet.y_head_rot,
                metadata=packet.unpack.copy(),
                properties=packet.synched_properties.copy(),
                links=packet.links.copy(),
            )
            self.add_actor(actor)
        elif id == 14:
            self.remove_actor(self.unique_ids.get(packet.entity_id))
        elif id == 17:
            self.remove_actor(packet.item_id)
            self.emit(ev.EventKind.take_item, lambda: ev.TakeItemEvent(packet.item_id, packet.actor_id))
        elif id in (18, 111):
            move = packet.move_data
            actor = self.actors.get(move.runtime_id)
            if actor is None:
                return
            if id == 18:
                position = vec(move.pos)
                pitch, yaw, head = (getattr(move, field) * 360 / 256 for field in ("rot_x", "rot_y", "rot_y_head"))
            else:
                position = actor.position
                coordinates = {}
                angles = [actor.pitch, actor.yaw, actor.head_yaw]
                for i, field in enumerate(
                    ("new_position_x", "new_position_y", "new_position_z", "rot_x", "rot_y", "rot_y_head")
                ):
                    value = getattr(move, field)
                    present = value is not None if self.options.protocol == 2193 else bool(move.header & (1 << i))
                    if present:
                        if i < 3:
                            coordinates[("x", "y", "z")[i]] = value
                        else:
                            angles[i - 3] = value * 360 / 256
                if coordinates:
                    position = replace(position, **coordinates)
                pitch, yaw, head = angles
            self.move_actor(actor, position, pitch, yaw, head)
        elif id == 39:
            actor = self.actors.get(packet.id)
            if actor:
                entries = {entry.id: entry for entry in actor.metadata.data} if actor.metadata is not None else {}
                entries.update((entry.id, entry.copy()) for entry in packet.packed_items.data)
                ints = (
                    {entry.property_index: entry for entry in actor.properties.int_entries}
                    if actor.properties is not None
                    else {}
                )
                floats = (
                    {entry.property_index: entry for entry in actor.properties.float_entries}
                    if actor.properties is not None
                    else {}
                )
                ints.update((entry.property_index, entry.copy()) for entry in packet.synched_properties.int_entries)
                floats.update((entry.property_index, entry.copy()) for entry in packet.synched_properties.float_entries)
                actor = replace(
                    actor,
                    metadata=p.SynchedActorData.CopyableDataList(data=entries.values()),
                    properties=p.PropertySyncData(int_entries=ints.values(), float_entries=floats.values()),
                )
                self.actors[packet.id] = actor
                self.emit(ev.EventKind.actor, lambda: ev.ActorEvent(ev.ActorEvent.Kind.metadata, actor))
        elif id in (49, 50):
            inventory_id = int(packet.inventory_id)
            if id == 49:
                inventory = InventorySnapshot(
                    {i: item.copy() for i, item in enumerate(packet.slots)},
                    True,
                    owned(packet.full_container_name),
                    owned(packet.storage_item),
                )
            else:
                previous = self.inventories.get(inventory_id)
                if previous is None:
                    previous = InventorySnapshot({})
                slots = dict(previous.slots)
                slots[packet.slot] = packet.item.copy()
                inventory = InventorySnapshot(
                    slots,
                    previous.complete,
                    owned(packet.full_container_name)
                    if packet.full_container_name is not None
                    else previous.full_container_name,
                    owned(packet.storage_item) if packet.storage_item is not None else previous.storage_item,
                )
            self.inventories[inventory_id] = inventory
            self.note_item_update(inventory_id, None if id == 49 else packet.slot)
            if id == 50:
                self.emit(
                    ev.EventKind.inventory_slot,
                    lambda: ev.InventorySlotEvent(self.id, inventory_id, packet.slot, inventory.slots[packet.slot]),
                )
            self.emit(ev.EventKind.inventory, lambda: ev.InventoryEvent(inventory_id, inventory))
        elif id == 30:
            if packet.transaction.index != 0:
                return
            changed = []
            for action in packet.transaction.value.transaction.actions:
                if (
                    action.source.type != p.InventorySourceType.CONTAINER_INVENTORY
                    or action.source.container_id is None
                ):
                    continue
                inventory_id = int(action.source.container_id)
                previous = self.inventories.get(inventory_id)
                if previous is None:
                    previous = InventorySnapshot({})
                slots = dict(previous.slots)
                slots[action.slot] = action.to_item_descriptor.copy()
                self.inventories[inventory_id] = replace(previous, slots=slots)
                self.note_item_update(inventory_id, action.slot)
                if inventory_id not in changed:
                    changed.append(inventory_id)
            for inventory_id in changed:
                self.emit(
                    ev.EventKind.inventory, lambda: ev.InventoryEvent(inventory_id, self.inventories[inventory_id])
                )
        elif id == 46:
            self.open_container(packet)
        elif id == 80:
            self.update_trade(packet)
        elif id == 47:
            if self.window and self.window.window_id == int(packet.container_id):
                ref = self.window
                self.invalidate_container()
                self.invalidate_operation("container closed")
                self.emit(ev.EventKind.container, lambda: ev.ContainerEvent(None, ref, True))
        elif id == 148:
            operation = self.pending
            if operation is not None and operation.phase == ItemOperationPhase.submitted:
                for response in packet.responses:
                    if self.pending is operation and response.client_request_id.raw_id == operation.key.request_id:
                        outcome, reason = ItemRequestOutcome.rejected, "server rejected item request"
                        if int(response.result) == 0:
                            try:
                                _inventory.reconcile(self, response)
                                outcome, reason = ItemRequestOutcome.completed, ""
                            except RuntimeError as error:
                                outcome, reason = ItemRequestOutcome.indeterminate, str(error)
                        self.finish_operation(outcome, reason, response.copy())
                    if not self.usable():
                        return
        elif id == 27:
            actor = self.actors.get(packet.runtime_id)
            if actor:
                self.actors[packet.runtime_id] = replace(actor, last_event=packet.copy())
            if packet.event_id == p.ActorEvent.DEATH:
                if not self.start_game:
                    self.defer_self_state(_InitialHealth(packet.runtime_id, 0))
                elif packet.runtime_id == self.status.actor_runtime_id:
                    self.health(0)
            self.emit(ev.EventKind.actor_signal, lambda: ev.ActorSignalEvent(packet.copy()))
        elif id == 45:
            if packet.state == p.PlayerRespawnState.READY_TO_SPAWN and self.respawn_phase == 1:
                self.position(vec(packet.pos), False, False, False)
                if self.usable():
                    self.action(True)
                    self.respawn_phase = 2
        elif id == 61:
            position = vec(packet.pos)
            if not finite(position):
                raise _MalformedPacket("non-finite dimension position")
            self.input = self.look = None
            self.input_epoch += 1
            self.equipment_epoch += 1
            self.invalidate_container()
            self.actors.clear()
            self.unique_ids.clear()
            self.status = replace(self.status, position=position, dimension=packet.dimension_id, selected_hotbar=None)
            self.loading_screen_id = packet.loading_screen_id
            self.dimension_deadline = self.runtime._loop.time() + self.options.spawn_timeout_ms / 1000
            self.invalidate_operation("dimension changed")
            self.emit(ev.EventKind.dimension, lambda: ev.DimensionEvent(packet.dimension_id, position))
            if self.usable() and self.options.load_screen:
                self.enqueue(
                    p.ServerboundLoadingScreenPacket(
                        loading_screen_packet_type=p.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN,
                        loading_screen_id=packet.loading_screen_id,
                    )
                )
        elif id == 36:
            if (
                self.dimension_deadline is not None
                and packet.action == p.PlayerActionType.CHANGE_DIMENSION_ACK
                and packet.runtime_id == self.status.actor_runtime_id
            ):
                self.dimension_deadline = None
                if self.options.load_screen:
                    self.enqueue(
                        p.ServerboundLoadingScreenPacket(
                            loading_screen_packet_type=p.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN,
                            loading_screen_id=self.loading_screen_id,
                        )
                    )
                self.action(False)
                self.handled_teleport = True
        elif id == 115:
            if packet.from_server:
                self.enqueue(p.NetworkStackLatencyPacket(create_time=packet.create_time, from_server=False))
            elif self.probe:
                stamp, sent = self.probe
                if packet.create_time in (stamp, stamp * 1000, stamp * 1000000) or packet.create_time * 1000 == stamp:
                    self.probe = None
                    elapsed = int((self.runtime._loop.time() - sent) * 1000000)
                    self.emit(ev.EventKind.latency, lambda: ev.LatencyEvent(elapsed))

    async def ticker(self):
        while self.usable():
            await asyncio.sleep(0.05 if self.next_tick is None else max(0, self.next_tick - self.runtime._loop.time()))
            now = self.runtime._loop.time()
            if self.pending is not None and self.operation_deadline is not None and now >= self.operation_deadline:
                self.finish_operation(ItemRequestOutcome.indeterminate, "item request timed out")
            if not self.usable():
                return
            if self.status.state != BotState.ready:
                if now >= self.spawn_deadline:
                    raise TimeoutError("login/spawn deadline expired")
                continue
            if self.dimension_deadline is not None and now >= self.dimension_deadline:
                raise TimeoutError("dimension change confirmation deadline expired")
            if self.next_tick is not None and now >= self.next_tick:
                lateness = now - self.next_tick
                self.next_tick += (math.floor(lateness / 0.05) + 1) * 0.05
                if self.next_tick <= now:
                    self.next_tick += 0.05
                self.emit(ev.EventKind.tick, lambda: ev.TickEvent(self.status.tick + 1, int(lateness * 1000000)))
                if self.usable():
                    self.tick()

    def tick(self):
        if self.dimension_deadline is not None:
            return
        frame = self.input or Input(position=self.status.position)
        self.input = None
        if self.look is not None:
            frame = replace(frame, pitch=self.look.x, yaw=self.look.y)
        p = self.p
        flags = []
        for enabled, flag in (
            (frame.move.y > 0, "UP"),
            (frame.move.y < 0, "DOWN"),
            (frame.move.x > 0, "RIGHT"),
            (frame.move.x < 0, "LEFT"),
            (frame.vertical_collision, "VERTICAL_COLLISION"),
            (frame.jumping, "JUMPING"),
            (frame.sneaking, "SNEAKING"),
            (frame.sprinting, "SPRINTING"),
            (self.handled_teleport, "HANDLED_TELEPORT"),
        ):
            if enabled:
                flags.append(getattr(p.PlayerAuthInputPacket.InputData, flag))
        for flag in frame.flags:
            value = getattr(p.PlayerAuthInputPacket.InputData, flag)
            if value not in flags:
                flags.append(value)
        if self.options.protocol == 1001:
            flags = sum(1 << int(flag) for flag in flags)
        tick = self.status.tick + 1
        rot = p.Vec2(x=frame.pitch, y=frame.yaw)
        move = p.Vec2(x=frame.move.x, y=frame.move.y)
        packet = p.PlayerAuthInputPacket(
            rot=rot,
            pos=self.wire_vec(frame.position),
            move=move,
            y_head_rot=frame.yaw,
            input_data=flags,
            input_mode=p.InputMode.MOUSE,
            play_mode=p.ClientPlayMode.SCREEN,
            new_interaction_model=p.NewInteractionModel.CROSSHAIR,
            interact_rotation=rot,
            client_tick=tick,
            pos_delta=self.wire_vec(frame.delta),
            item_use_transaction=None,
            item_stack_request=None,
            player_block_actions=None,
            vehicle_rot=None,
            client_predicted_vehicle=None,
            analog_move_vector=move,
            camera_orientation=p.Vec3(x=0, y=0, z=0),
            raw_move_vector=move,
        )
        epoch = self.input_epoch
        self.enqueue(packet, valid=lambda: epoch == self.input_epoch)
        self.handled_teleport = False
        self.status = replace(self.status, tick=tick, position=frame.position)

    def wire_vec(self, value):
        return self.p.Vec3(x=value.x, y=value.y, z=value.z)

    def defer_self_state(self, effect):
        if len(self.initial_self_state) >= 4096:
            raise _MalformedPacket("pre-StartGame self-state limit exceeded")
        self.initial_self_state.append(effect)

    def apply_initial_self_state(self):
        while self.initial_self_state and self.usable():
            effect = self.initial_self_state.popleft()
            if effect.runtime_id is not None and effect.runtime_id != self.status.actor_runtime_id:
                continue
            if isinstance(effect, _InitialPosition):
                self.position(effect.position, effect.teleport, False, effect.acknowledge)
            else:
                self.health(effect.value)

    def set_input(self, frame):
        self.require_ready()
        if not finite(frame.position, frame.delta, frame.move, frame.pitch, frame.yaw):
            raise ValueError("input values must be finite")
        try:
            flags = tuple(frame.flags)
        except TypeError:
            raise ValueError("input flags must be a sequence of native flag names") from None
        enum = self.p.PlayerAuthInputPacket.InputData
        reserved = {
            "INPUT_NUM", "HANDLED_TELEPORT", "VERTICAL_COLLISION",
            "PERFORM_ITEM_INTERACTION", "PERFORM_BLOCK_ACTIONS",
            "PERFORM_ITEM_STACK_REQUEST", "IS_IN_CLIENT_PREDICTED_VEHICLE",
            "INTERNAL_UPDATE",
        }
        if any(
            type(flag) is not str or not flag.isupper() or flag in reserved or not hasattr(enum, flag)
            for flag in flags
        ):
            raise ValueError("invalid or state-owned PlayerAuthInput flag for this protocol")
        if type(frame) is Input:
            self.input = Input(
                frame.position, frame.delta, frame.move, frame.pitch, frame.yaw,
                frame.vertical_collision, frame.jumping, frame.sneaking, frame.sprinting, flags,
            )
        else:
            self.input = replace(frame, flags=flags)

    def set_look(self, pitch, yaw):
        if not finite(pitch, yaw):
            raise ValueError("look values must be finite")
        self.look = Vec2(pitch, yaw)

    def position(self, position, teleport, correction, acknowledge):
        if not finite(position):
            raise _MalformedPacket("non-finite own position")
        self.status = replace(self.status, position=position)
        self.input = None
        self.input_epoch += 1
        if acknowledge:
            self.handled_teleport = True
        if correction:
            self.stats["corrections"] += 1
        self.emit(ev.EventKind.position, lambda: ev.PositionEvent(position, teleport, correction))

    def health(self, value):
        if not finite(value):
            raise _MalformedPacket("non-finite health")
        died = value <= 0 and self.status.alive
        respawned = value > 0 and not self.status.alive and self.respawn_phase == 2
        if died:
            self.status = replace(self.status, health=value, alive=False, selected_hotbar=None)
        elif respawned:
            self.status = replace(self.status, health=value, alive=True)
        else:
            self.status = replace(self.status, health=value)
        if died:
            self.look = self.input = None
            self.input_epoch += 1
            self.equipment_epoch += 1
            self.invalidate_container()
            self.inventories.clear()
            self.respawn_phase = 0
            self.stats["deaths"] += 1
            self.invalidate_operation("player died")
            self.emit(ev.EventKind.death, ev.DeathEvent)
        elif respawned:
            self.respawn_phase = 0
            self.stats["respawns"] += 1
            self.emit(ev.EventKind.respawn, ev.RespawnEvent)
        self.emit(ev.EventKind.health, lambda: ev.HealthEvent(value))

    def move_actor(self, actor, position, pitch, yaw, head):
        if not finite(position, pitch, yaw, head):
            raise _MalformedPacket("non-finite actor motion")
        actor = replace(actor, position=position, pitch=pitch, yaw=yaw, head_yaw=head)
        self.actors[actor.runtime_id] = actor
        self.emit(
            ev.EventKind.actor_motion,
            lambda: ev.ActorMotionEvent(
                self.id,
                actor.runtime_id,
                actor.unique_id,
                actor.position,
                actor.velocity,
                actor.pitch,
                actor.yaw,
                actor.head_yaw,
                actor.body_yaw,
            ),
        )
        self.emit(ev.EventKind.actor, lambda: ev.ActorEvent(ev.ActorEvent.Kind.moved, actor))

    def add_actor(self, actor):
        if not finite(actor.position):
            raise _MalformedPacket("non-finite actor position")
        old = self.actors.get(actor.runtime_id)
        other = self.unique_ids.get(actor.unique_id)
        if other is not None and other != actor.runtime_id:
            self.remove_actor(other)
        if old is not None and old.unique_id != actor.unique_id:
            self.remove_actor(old.runtime_id)
        if not self.usable():
            return
        self.actors[actor.runtime_id] = actor
        self.unique_ids[actor.unique_id] = actor.runtime_id
        self.emit(ev.EventKind.actor, lambda: ev.ActorEvent(ev.ActorEvent.Kind.added, actor))

    def remove_actor(self, runtime_id):
        actor = self.actors.pop(runtime_id, None)
        if actor is None:
            return
        self.unique_ids.pop(actor.unique_id, None)
        if self.interacting == actor.unique_id or self.window and self.window.trader_unique_id == actor.unique_id:
            self.invalidate_container()
            self.invalidate_operation("trader removed")
        self.emit(ev.EventKind.actor, lambda: ev.ActorEvent(ev.ActorEvent.Kind.removed, actor))

    def submit_item_request(self, make_packet, *, window=None):
        self.runtime._guard()
        self.require_interaction()
        if self.pending is not None:
            raise RuntimeError("an item request is pending")
        if window is not None and (
            window != self.window or window.session_id != self.bot._generation
        ):
            raise RuntimeError("stale or foreign container window")
        if self.next_request_id < -2147483647:
            raise RuntimeError("item request IDs exhausted")
        key = ItemRequestKey(self.bot._token, self.id, self.next_request_id, window)
        packet = make_packet(key.request_id)
        if (
            not isinstance(packet, self.p.ItemStackRequestPacket)
            or len(packet.requests) != 1
            or packet.requests[0].client_request_id.raw_id != key.request_id
        ):
            raise ValueError("typed request must contain the assigned request ID exactly once")
        operation = ItemOperation(key, self.runtime._loop)
        self.enqueue(packet, operation=operation)
        self.pending = operation
        self.next_request_id -= 2
        return operation

    def note_item_update(self, inventory_id, index):
        if self.pending is not None and self.pending.phase == ItemOperationPhase.submitted:
            self.item_authority.update(change.physical for change in self.item_changes
                                       if change.physical[0] == inventory_id
                                       and (index is None or change.physical[1] == index))

    def begin_move_item(self, source, destination, count):
        return _inventory.move(self, source, destination, count)

    def begin_trade(self, offer, destination):
        return _inventory.trade(self, offer, destination)

    def get_trade_offers(self, window):
        return _inventory.offers(self, window)

    def finish_operation(self, outcome, reason, response=None):
        operation = self.pending
        if operation is None:
            return
        if outcome == ItemRequestOutcome.indeterminate:
            for change in self.item_changes:
                inventory_id, index = change.physical
                inventory = self.inventories.get(inventory_id)
                if inventory is not None:
                    slots = dict(inventory.slots)
                    slots.pop(index, None)
                    self.inventories[inventory_id] = replace(inventory, slots=slots, complete=False)
        touched = []
        for change in self.item_changes:
            try:
                touched.append(self.get_item(change.slot))
            except RuntimeError:
                touched.append(SlotObservation(change.slot))
        result = ItemOperationResult(
            operation.key, outcome, None if response is None else int(response.result), tuple(touched), response, reason
        )
        self.pending = None
        self.item_changes = ()
        self.item_authority.clear()
        self.operation_deadline = None
        operation._finish(result)
        self.emit(ev.EventKind.item_request, lambda: ev.ItemRequestEvent(result.key, outcome, response, reason))

    def invalidate_operation(self, reason):
        if self.pending is not None:
            outcome = (
                ItemRequestOutcome.indeterminate
                if self.pending.phase == ItemOperationPhase.submitted
                else ItemRequestOutcome.cancelled
            )
            self.finish_operation(outcome, reason)

    def cancel_operation(self, operation):
        if operation.key.bot is not self.bot._token or operation.key.session_id != self.bot._generation:
            raise RuntimeError("stale or foreign item operation")
        if operation.phase == ItemOperationPhase.terminal:
            return ItemCancellation.already_terminal
        if operation is not self.pending:
            raise RuntimeError("item operation is not pending")
        if operation.phase == ItemOperationPhase.submitted:
            return ItemCancellation.already_submitted
        self.finish_operation(ItemRequestOutcome.cancelled, "item operation cancelled")
        return ItemCancellation.cancelled

    def require_ready(self):
        if not self.usable() or self.status.state != BotState.ready:
            raise RuntimeError("bot is not ready")

    def require_interaction(self):
        self.require_ready()
        if not self.status.alive:
            raise RuntimeError("bot is dead")
        if self.dimension_deadline is not None:
            raise RuntimeError("dimension is changing")
        if self.container and self.container.closing:
            raise RuntimeError("container is closing")

    def held_item(self):
        slot = self.status.selected_hotbar
        inventory = self.inventories.get(0)
        if slot is None or inventory is None or slot not in inventory.slots:
            raise RuntimeError("hotbar item or selection is unknown")
        item = unwrap(inventory.slots[slot])
        if not isinstance(item, self.p.SerializedNetworkItemStackDescriptor):
            raise RuntimeError("hotbar descriptor version mismatch")
        return item

    def select_hotbar(self, slot):
        self.require_interaction()
        if self.pending is not None:
            raise RuntimeError("an item request is pending")
        if type(slot) is not int or not 0 <= slot <= 8:
            raise ValueError("hotbar slot must be in 0..8")
        inventory = self.inventories.get(0)
        item = None if inventory is None else unwrap(inventory.slots.get(slot))
        if not isinstance(item, self.p.SerializedNetworkItemStackDescriptor):
            raise RuntimeError("hotbar item is unknown or descriptor version mismatch")
        epoch = self.equipment_epoch
        self.enqueue(
            self.p.MobEquipmentPacket(
                runtime_id=self.status.actor_runtime_id,
                item=item,
                slot=slot,
                selected_slot=slot,
                container_id=self.p.ContainerID.INVENTORY,
            ),
            valid=lambda: epoch == self.equipment_epoch,
        )
        self.status = replace(self.status, selected_hotbar=slot)

    def interact(self, entity, hit, attack):
        actor = self.actors.get(entity.runtime_id)
        if (
            entity.bot is not self.bot._token
            or entity.session_id != self.bot._generation
            or actor is None
            or actor.unique_id != entity.unique_id
        ):
            raise RuntimeError("stale or foreign entity")
        self.require_interaction()
        if self.pending is not None:
            raise RuntimeError("an item request is pending")
        if not finite(hit):
            raise ValueError("hit position must be finite")
        p = self.p
        item = self.held_item()
        transaction = p.ItemUseOnActorInventoryTransaction(
            transaction=p.InventoryTransaction(actions=[]),
            runtime_id=entity.runtime_id,
            action_type=p.ItemUseOnActorInventoryTransaction.ActionType.ATTACK
            if attack
            else p.ItemUseOnActorInventoryTransaction.ActionType.INTERACT,
            slot=self.status.selected_hotbar,
            item=item,
            from_pos=self.wire_vec(self.status.position),
            hit_pos=self.wire_vec(hit),
        )
        packet = p.InventoryTransactionPacket(
            legacy_request_id=0, legacy_set_item_slots=None, transaction=bp.Variant(3, transaction)
        )
        epoch = self.input_epoch
        self.enqueue(
            packet,
            valid=lambda: (
                epoch == self.input_epoch
                and self.actors.get(entity.runtime_id) is not None
                and self.actors[entity.runtime_id].unique_id == entity.unique_id
            ),
        )
        if not attack:
            self.interacting = entity.unique_id

    def open_container(self, packet):
        if not self.status.alive or self.dimension_deadline is not None or self.container and self.container.closing:
            return
        identity = (int(packet.container_id), int(packet.type), packet.entity_unique_id)
        if self.window is not None and identity != (
            self.window.window_id,
            self.window.container_type,
            self.window.trader_unique_id,
        ):
            return
        if self.window is None:
            self.window_generation += 1
            self.window = WindowRef(self.bot._token, self.id, self.window_generation, *identity)
        self.container = replace(self.container or ContainerSnapshot(), open=packet.copy())
        if self.pending is not None and self.pending.key.window != self.window:
            self.invalidate_operation("trade window changed")
        self.emit(ev.EventKind.container, lambda: ev.ContainerEvent(self.container, self.window))

    def update_trade(self, packet):
        identity = (int(packet.container_id), int(packet.type), packet.entity_unique_id)
        if (
            not self.status.alive
            or self.dimension_deadline is not None
            or identity[1] != 15
            or not 1 <= identity[0] <= 100
        ):
            return
        if self.container and self.container.closing or identity[2] not in self.unique_ids:
            return
        if self.window is not None:
            if identity != (self.window.window_id, self.window.container_type, self.window.trader_unique_id):
                return
        elif self.interacting != identity[2]:
            return
        else:
            self.window_generation += 1
            self.window = WindowRef(self.bot._token, self.id, self.window_generation, *identity)
        self.offer_revision += 1
        self.container = replace(self.container or ContainerSnapshot(), trade=packet.copy())
        self.interacting = None
        if self.pending is not None and self.pending.key.window != self.window:
            self.invalidate_operation("trade window changed")
        self.emit(ev.EventKind.container, lambda: ev.ContainerEvent(self.container, self.window))

    def validate_window(self, window):
        if (
            window is None
            or window != self.window
            or window.session_id != self.bot._generation
            or window.container_type != 15
        ):
            raise RuntimeError("stale or foreign trade window")

    def get_trade_window(self):
        if self.window is None or self.window.container_type != 15 or not 1 <= self.window.window_id <= 100:
            return None
        return TradeWindowObservation(self.window, self.container.closing, self.offer_revision)

    def invalidate_container(self):
        if self.window is not None:
            self.inventories.pop(self.window.window_id, None)
        self.inventories.pop(124, None)
        self.window = self.container = None
        self.offer_revision = 0
        self.interacting = None

    def close_container(self, window):
        self.validate_window(window)
        self.require_interaction()
        p = self.p
        self.enqueue(
            p.ContainerClosePacket(
                container_id=p.ContainerID(window.window_id),
                container_type=p.ContainerType(window.container_type),
                server_initiated_close=False,
            ),
            valid=lambda: self.window == window,
        )
        self.container = replace(self.container, closing=True)
        self.interacting = None
        self.invalidate_operation("container closing")

    def trade_input(self, window, index):
        self.validate_window(window)
        if type(index) is not int or not 0 <= index <= 1:
            raise ValueError("trade input slot must be in 0..1")
        return InventorySlot(self.bot._token, self.id, InventoryArea.trade_input, index, window)

    def request_chunk_radius(self, radius):
        self.require_ready()
        if type(radius) is not int or not 1 <= radius <= 96:
            raise ValueError("chunk radius must be in 1..96")
        self.enqueue(self.p.RequestChunkRadiusPacket(chunk_radius=radius, max_chunk_radius=radius))

    def require_spawning(self):
        if not self.usable() or self.status.state not in (BotState.spawning, BotState.ready):
            raise RuntimeError("bot is not spawning or ready")

    def report_blob_status(self, missing, present):
        self.require_spawning()
        self.enqueue(self.p.ClientCacheBlobStatusPacket(missing_ids=missing, found_ids=present))

    def request_sub_chunks(self, dimension, chunk_x, chunk_z, heights):
        self.require_spawning()
        p = self.p
        self.enqueue(
            p.SubChunkRequestPacket(
                dimension_type=dimension,
                center_pos=p.SubChunkPos(x=chunk_x, y=0, z=chunk_z),
                sub_chunk_pos_offsets=[p.SubChunkPacket.SubChunkPosOffset(x=0, y=h, z=0) for h in heights],
            )
        )

    def action(self, respawn):
        p = self.p
        zero = p.BlockPos(x=0, y=0, z=0)
        self.enqueue(
            p.PlayerActionPacket(
                runtime_id=self.status.actor_runtime_id,
                action=p.PlayerActionType.RESPAWN if respawn else p.PlayerActionType.CHANGE_DIMENSION_ACK,
                pos=zero,
                result_pos=zero,
                face=-1,
            )
        )

    def respawn(self, *, retry: bool = False):
        if type(retry) is not bool:
            raise ValueError("retry must be a boolean")
        self.require_ready()
        if self.status.alive:
            raise RuntimeError("bot is alive")
        if self.respawn_phase and not retry:
            return
        if self.respawn_phase == 2:
            self.action(True)
            return
        self.enqueue(
            self.p.RespawnPacket(
                pos=self.wire_vec(Vec3()),
                state=self.p.PlayerRespawnState.CLIENT_READY_TO_SPAWN,
                runtime_id=self.status.actor_runtime_id,
            )
        )
        self.respawn_phase = 1

    def probe_latency(self):
        self.require_ready()
        stamp = max(time.time_ns() // 1000000, self.last_probe_stamp + 1)
        if not 0 <= stamp <= ((1 << 63) - 1) // 1000000:
            raise RuntimeError("latency timestamp out of range")
        self.enqueue(self.p.NetworkStackLatencyPacket(create_time=stamp, from_server=False))
        self.last_probe_stamp = stamp
        if self.probe is not None:
            self.stats["probes_lost"] += 1
        self.probe = (stamp, self.runtime._loop.time())
        self.stats["probes_sent"] += 1

    def player_slot(self, index):
        if type(index) is not int or not 0 <= index <= 35:
            raise ValueError("player inventory slot must be in 0..35")
        return InventorySlot(self.bot._token, self.id, InventoryArea.player, index)

    def get_item(self, slot):
        if slot.bot is not self.bot._token or slot.session_id != self.bot._generation:
            raise RuntimeError("stale or foreign inventory slot")
        inventory_id = 0
        if slot.area == InventoryArea.trade_input:
            self.validate_window(slot.window)
            inventory_id = slot.window.window_id
        inventory = self.inventories.get(inventory_id)
        item = None if inventory is None else inventory.slots.get(slot.index)
        name = None
        if item is not None and item.id:
            matches = [entry for entry in self.registry if entry.id == item.id]
            if (
                len(matches) == 1
                and matches[0].name
                and sum(entry.name == matches[0].name for entry in self.registry) == 1
            ):
                name = matches[0].name
        return SlotObservation(slot, item, name)

    def count_player_item(self, identifier):
        observed = [self.get_item(self.player_slot(i)) for i in range(36)]
        return ItemCount(
            self.id,
            sum(item.count for item in observed if item.identifier == identifier),
            all(
                item.state == SlotState.empty or (item.state == SlotState.occupied and item.identifier is not None)
                for item in observed
            ),
        )
