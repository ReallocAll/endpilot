"""Network settings and the opening Bedrock player join exchange."""

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from functools import partial
from typing import Callable, Literal
from uuid import UUID

from bedrock_protocol import DecodeError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ._join_address import _JoinAddress
from .authentication import DEFAULT_LOCALE, _OnlineIdentity, _validate_locale
from .beds import invalidates_bed_respawn
from .block_data import matches_vanilla_palette
from .client_clock import ClientClock
from .compression import PacketConnection
from .creative_inventory import creative_packet_ids
from .dimension_transition import _DimensionTransitions
from .entity_registry import EntityRegistrySnapshot, _read_entity_registry
from .forms import FORM_PACKET_IDS
from .item_registry import ItemRegistrySnapshot
from .player_state import VITAL_ATTRIBUTES, air_metadata, movement_speed_attribute, swimming_metadata
from .protocol import Protocol, RawPacket, read_batch, write_batch
from .qa import PacketTimeline
from .resource_packs import PackDecisionHandler, ResourcePackExchange, ResourcePackIndex
from .scoreboard import SCOREBOARD_PACKET_IDS
from .world_environment import world_environment_packet_ids


class LoginRefused(ConnectionError):
    """The server explicitly rejected a player before spawn."""

    reason_name: str | None = None
    server_message: str | None = None

    @classmethod
    def _from_packet(cls, detail: str, packet) -> "LoginRefused":
        error = cls(detail)
        reason = getattr(packet, "reason", getattr(packet, "status", None))
        error.reason_name = getattr(reason, "name", None)
        error.server_message = getattr(getattr(packet, "messages", None), "message", None)
        return error


@dataclass(frozen=True, slots=True)
class SessionEnd:
    """The first observed end of a joined session."""

    kind: Literal["server_disconnect", "reader_end", "local_close"]
    reason_code: int | None = None
    reason_name: str | None = None
    message: str | None = None
    filtered_message: str | None = None
    error_type: str | None = None


class _SessionEndState:
    """A result and its waiters with no reference to the session or its transport."""

    def __init__(self):
        self.result: SessionEnd | None = None
        self._ready = asyncio.Event()

    def finish(self, result: SessionEnd) -> bool:
        if self.result is not None:
            return False
        self.result = result
        self._ready.set()
        return True

    async def wait(self, timeout: float | None) -> SessionEnd:
        async with asyncio.timeout(timeout):
            await self._ready.wait()
        assert self.result is not None
        return self.result


class _PendingOnlineJoin:
    """A join owns its task and transport up to session handoff."""

    def __init__(self):
        self.task: asyncio.Task[None] | None = None
        self.transport = None
        self.transport_close_error: BaseException | None = None

    def take_transport(self):
        transport, self.transport = self.transport, None
        return transport


async def _close_after_failed_join(resource, error: BaseException, activity: str) -> BaseException | None:
    try:
        await resource.close()
    except BaseException as cleanup_error:
        if cleanup_error is not error:
            error.add_note(f"{activity}: {cleanup_error!r}")
            for note in getattr(cleanup_error, "__notes__", ()):
                error.add_note(note)
        return cleanup_error
    return None


def _validate_chunk_radius(radius: int) -> None:
    if type(radius) is not int or not 1 <= radius <= 96:
        raise ValueError("chunk_radius must be in 1..96")


def _validate_join(protocol: Protocol, chunk_radius: int, locale: str, timeout: float, *, online: bool) -> None:
    if online and protocol.number != 2225:
        raise ValueError("online login requires protocol 2225")
    _validate_chunk_radius(chunk_radius)
    _validate_locale(locale)
    # Validate the existing timeout argument without starting its deadline.
    asyncio.timeout(timeout)


def _experiments_allow_vanilla_recipe_tags(experiments) -> bool:
    # Beta APIs (gametest) alone preserves vanilla recipe tags on BDS 1.26.66.
    return all(not toggle.enabled or toggle.name == "gametest" for toggle in experiments.toggles)


async def request_network_settings(connection, protocol: Protocol, *, timeline: PacketTimeline | None = None,
                                   clock: ClientClock | None = None):
    """Exchange the first game message on a newly opened batch transport."""
    request = protocol.binding.RequestNetworkSettingsPacket()
    request.client_network_version = protocol.number
    await connection.send(write_batch([protocol.encode(request)]))
    if timeline is not None:
        timeline._record_sent(type(request).__name__)

    while True:
        batch = await connection.recv()
        tick = clock.tick if timeline is not None and clock is not None else None
        for raw in read_batch(batch):
            if timeline is not None:
                timeline._record(protocol, raw, tick=tick)
            if raw.packet_id == protocol.binding.NetworkSettingsPacket.ID:
                return protocol.decode(raw)
            if raw.packet_id in (protocol.binding.DisconnectPacket.ID, protocol.binding.PlayStatusPacket.ID):
                packet = protocol.decode(raw)
                raise LoginRefused._from_packet(f"server refused network settings request: {packet!r}", packet)


def _verify_handshake(token: str):
    """The NetherNet transport is already encrypted; verify the server's signed handshake."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid server handshake token")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    header = json.loads(decode(parts[0]))
    if header.get("alg") != "ES384":
        raise ValueError("unexpected server handshake algorithm")
    public = serialization.load_der_public_key(base64.b64decode(header["x5u"]))
    if not isinstance(public, ec.EllipticCurvePublicKey):
        raise ValueError("invalid server handshake key")
    signature = decode(parts[2])
    if len(signature) != 96:
        raise ValueError("invalid handshake signature")
    r, s = int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
    public.verify(encode_dss_signature(r, s), ".".join(parts[:2]).encode(), ec.ECDSA(hashes.SHA384()))
    return public, base64.b64decode(json.loads(decode(parts[1]))["salt"])


class JoinSession:
    """Owns join progress and the established packet stream, not gameplay actions."""

    def __init__(self, packets: PacketConnection, identity, address: str, *, clock: ClientClock | None = None):
        self.packets = packets
        self.clock = ClientClock() if clock is None else clock
        self.identity = identity
        self.address = address
        self.player_id = None
        self.player_uuid: UUID | None = None
        self.player_unique_id = None
        self.player_game_type: str | None = None
        self._enchantment_seed_at_join: int | None = None
        self.reported_server_version: str | None = None
        self.dimension_id = 0
        self.chunk_radius = None
        self.requested_chunk_radius: int | None = None
        self.accepted_chunk_radius: int | None = None
        self._unsubscribe_chunk_radius = None
        self.spawned = False
        self.block_network_ids_are_hashes = False
        self.has_custom_blocks = False
        self.vanilla_recipe_tags = False
        self.item_names: dict[int, str] = {}
        self.item_registry: ItemRegistrySnapshot | None = None
        self._item_registry_subscriptions = ()
        self.entity_registry: EntityRegistrySnapshot | None = None
        self._entity_registry_subscriptions = ()
        self._end_state = _SessionEndState()
        self._end_subscriptions = ()
        self._end_observer: Callable[[SessionEnd], None] | None = None
        self.movement_speed: float | None = None
        self.bed_packets = []
        self.boss_bar_packets = []
        self.crafting_data_packets = []
        self.creative_packets = []
        self.inventory_packets = []
        self.player_state_packets = []
        self.command_catalog_packets: list[RawPacket] = []
        self.commands_enabled: bool | None = None
        self.movement_packets = []
        self.entity_packets = []
        self.form_packets = []
        self.scoreboard_packets = []
        self.environment_packets = []
        self.resource_pack_index: ResourcePackIndex | None = None
        self.terrain_packets = []
        self.replayed_dimension_packets: list[RawPacket] = []
        self._dimensions = _DimensionTransitions(packets)

    @property
    def enchantment_seed_at_join(self) -> int | None:
        """The signed enchantment seed from this session's StartGame, if received."""
        return self._enchantment_seed_at_join

    async def _receive_dimension_packet(self, raw):
        if self.player_id is not None and raw.packet_id in self._dimensions.packet_ids:
            await self._dimensions.receive(raw, self.player_id)

    @classmethod
    async def _connect_address(
        cls, target: _JoinAddress, identity, protocol: Protocol, timeout: float,
        *, server_key_sha256: str | None = None, **options,
    ):
        from importlib.util import find_spec

        if target.scheme in ("https", "http"):
            return await cls.connect_nethernet(
                target.value, identity, protocol, timeout, server_key_sha256=server_key_sha256, **options,
            )
        if target.scheme is None and (
            server_key_sha256 is not None or all(find_spec(module) is not None for module in ("nethernet", "aiohttp"))
        ):
            from .transport._nethernet_http import _NetherNetUnavailable

            for scheme in ("https", "http"):
                candidate = await cls.connect_nethernet(
                    f"{scheme}://{target.authority}", identity, protocol, timeout,
                    server_key_sha256=server_key_sha256, _check_endpoint=True, **options,
                )
                if not isinstance(candidate, _NetherNetUnavailable):
                    return candidate
                if scheme == "http" and server_key_sha256 is not None:
                    raise candidate.error
        return await cls.connect_raknet(
            target.host, target.port, identity, protocol, timeout, _address=target.authority, **options,
        )

    @classmethod
    async def connect_nethernet(
        cls, url: str, identity, protocol: Protocol, timeout: float = 30,
        *, server_key_sha256: str | None = None, timeline: PacketTimeline | None = None,
        chunk_radius: int = 8, locale: str = DEFAULT_LOCALE, load_screen: bool = True,
        retain_initial_chunks: bool = False,
        clock: ClientClock | None = None,
        on_resource_packs: PackDecisionHandler | None = None,
        _online_join: _PendingOnlineJoin | None = None,
        _check_endpoint: bool = False,
    ):
        from nethernet import ConnectionClosed

        from .transport.nethernet import connect

        _validate_chunk_radius(chunk_radius)
        credentials = {}
        if isinstance(identity, _OnlineIdentity):
            credentials = {"multiplayer_token": identity.multiplayer_token, "issuer": identity.issuer}
        if _check_endpoint:
            credentials["_check_endpoint"] = True
        return await cls._connect_transport(
            partial(connect, url, private_key=identity.key, server_key_sha256=server_key_sha256, **credentials),
            identity, url, protocol, timeout, timeline, clock, _online_join=_online_join,
            _retry_before_link=_check_endpoint, _transport_closed=ConnectionClosed,
            chunk_radius=chunk_radius, locale=locale, load_screen=load_screen,
            retain_initial_chunks=retain_initial_chunks, on_resource_packs=on_resource_packs,
        )

    @classmethod
    async def connect_raknet(
        cls, host: str, port: int, identity, protocol: Protocol, timeout: float = 30,
        *, timeline: PacketTimeline | None = None, clock: ClientClock | None = None,
        chunk_radius: int = 8, locale: str = DEFAULT_LOCALE, retain_initial_chunks: bool = False,
        on_resource_packs: PackDecisionHandler | None = None,
        _online_join: _PendingOnlineJoin | None = None,
        _address: str | None = None,
    ):
        from .transport.raknet import connect

        _validate_chunk_radius(chunk_radius)
        return await cls._connect_transport(
            partial(connect, host, port), identity, _address or f"{host}:{port}", protocol, timeout, timeline, clock,
            _online_join=_online_join, chunk_radius=chunk_radius, locale=locale,
            retain_initial_chunks=retain_initial_chunks, on_resource_packs=on_resource_packs,
        )

    @classmethod
    async def _connect_transport(
        cls, dial, identity, address, protocol, timeout, timeline, clock,
        *, _online_join: _PendingOnlineJoin | None, _retry_before_link: bool = False,
        _transport_closed: type[Exception] | None = None, **join_options,
    ):
        transport = None
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                transport = await dial()
                if _online_join is not None:
                    _online_join.transport = transport
                session = await cls._join_connected(
                    transport, identity, address, protocol, timeline, clock,
                    _online_join=_online_join, **join_options,
                )
            if _online_join is not None:
                _online_join.take_transport()
            return session
        except BaseException as error:
            if timeline is not None and _transport_closed is not None and isinstance(error, _transport_closed):
                timeline._record_transport_failure(error)
            if _retry_before_link and transport is None:
                from .transport._nethernet_http import _NetherNetUnavailable

                if isinstance(error, _NetherNetUnavailable):
                    return error
                if isinstance(error, TimeoutError) and deadline.expired():
                    cancelled = error.__cause__
                    # The deadline can interrupt the library while it is unwinding a refusal.
                    if isinstance(cancelled, asyncio.CancelledError) and cancelled.__context__ is None:
                        return _NetherNetUnavailable(error)
            if _online_join is not None:
                transport = _online_join.take_transport()
            if transport is not None:
                cleanup_error = await _close_after_failed_join(transport, error, "transport cleanup after failed join")
                if _online_join is not None:
                    _online_join.transport_close_error = cleanup_error
            raise

    @classmethod
    async def _join_connected(
        cls, transport, identity, address, protocol, timeline, clock,
        *, _online_join: _PendingOnlineJoin | None = None, **join_options,
    ):
        if clock is None:
            clock = ClientClock()
        clock.start()
        try:
            if _online_join is not None:
                clock.get_tick()
            settings = await request_network_settings(transport, protocol, timeline=timeline, clock=clock)
            if _online_join is not None:
                clock.get_tick()
            packets = PacketConnection(transport, protocol, settings, timeline=timeline, clock=clock)
            session = cls(packets, identity, address, clock=clock)
            await session.join(**join_options)
            if _online_join is not None:
                clock.get_tick()
            session._item_registry_subscriptions = (
                packets.subscribe_raw(session._on_item_registry),
                packets.subscribe_reader_end(session._clear_item_registry),
            )
            session._entity_registry_subscriptions = (
                packets.subscribe_raw(session._on_entity_registry),
                packets.subscribe_reader_end(session._clear_entity_registry),
            )
            return session
        except BaseException as error:
            if timeline is not None and isinstance(error, ConnectionError):
                timeline._record_transport_failure(error)
            await _close_after_failed_join(clock, error, "clock cleanup after failed join")
            raise

    async def join(
        self, *, chunk_radius: int = 8, locale: str = DEFAULT_LOCALE, load_screen: bool = True,
        retain_initial_chunks: bool = False, on_resource_packs: PackDecisionHandler | None = None,
    ) -> None:
        packs = ResourcePackExchange(self.packets, on_resource_packs)
        try:
            await self._join(packs, chunk_radius=chunk_radius, locale=locale, load_screen=load_screen,
                             retain_initial_chunks=retain_initial_chunks)
        finally:
            packs.close()

    async def _join(
        self, packs: ResourcePackExchange, *, chunk_radius: int, locale: str, load_screen: bool,
        retain_initial_chunks: bool,
    ) -> None:
        protocol = self.packets.protocol
        binding = protocol.binding
        environment_ids = world_environment_packet_ids(binding)
        creative_ids = creative_packet_ids(binding)
        startup_chunks = []
        early_equipment = []
        startup_dimension_change = False
        stack_allows_vanilla_recipe_tags = False
        movement_speeds = {}
        login = binding.LoginPacket()
        login.client_network_version = protocol.number
        login.connection_request = self.identity.login_request(
            self.address, protocol=protocol.number, chunk_radius=chunk_radius, locale=locale,
        )
        # Bind later skin submissions to the UUID serialized in this login.
        self.player_uuid = self.identity.uuid
        await self.packets.send(login)

        while not self.spawned:
            raw = await self.packets.recv_raw()
            await self._receive_dimension_packet(raw)
            if raw.packet_id == binding.ServerToClientHandshakePacket.ID:
                server_key, salt = _verify_handshake(self.packets.protocol.decode(raw).token)
                if hasattr(self.packets.transport, "enable_encryption"):
                    key = hashlib.sha256(
                        salt + self.identity.key.exchange(ec.ECDH(), server_key)
                    ).digest()
                    self.packets.transport.enable_encryption(key)
                await self.packets.send(binding.ClientToServerHandshakePacket())
            elif raw.packet_id == binding.DisconnectPacket.ID:
                packet = self.packets.protocol.decode(raw)
                raise LoginRefused._from_packet(f"server rejected login: {packet!r}", packet)
            elif raw.packet_id == binding.PlayStatusPacket.ID:
                packet = self.packets.protocol.decode(raw)
                status = packet.status
                if int(status) == 3:
                    packs.check_ready()
                    if load_screen:
                        screen = binding.ServerboundLoadingScreenPacketType
                        for phase in (screen.START_LOADING_SCREEN, screen.END_LOADING_SCREEN):
                            await self.packets.send(binding.ServerboundLoadingScreenPacket(
                                serverbound_loading_screen_packet_type=phase, loading_screen_id=None
                            ))
                    # Initialize after PLAYER_SPAWN and loading-screen end,
                    # immediately before the first player input.
                    initialized = binding.SetLocalPlayerAsInitializedPacket()
                    initialized.player_id = self.player_id
                    await self.packets.send(initialized)
                    self.spawned = True
                elif int(status) != 0:
                    raise LoginRefused._from_packet(f"server refused login: {status}", packet)
            elif raw.packet_id in packs.packet_ids:
                packet = protocol.decode(raw)
                await packs.handle(packet)
                if protocol.number == 2225 and raw.packet_id == binding.ResourcePackStackPacket.ID:
                    stack_allows_vanilla_recipe_tags = (
                        packs.plain_pack_stack or _experiments_allow_vanilla_recipe_tags(packet.experiments)
                    )
            elif raw.packet_id in (binding.CraftingDataPacket.ID, binding.UnlockedRecipesPacket.ID):
                # Retain the recipe catalog and recipe-book updates in wire order.
                self.crafting_data_packets.append(raw)
            elif raw.packet_id in creative_ids:
                packet = protocol.decode(raw)
                if raw.packet_id == binding.CreativeContentPacket.ID:
                    self.creative_packets.append(packet)
                else:
                    # Game modes and abilities must be replayed in the same order.
                    self.player_state_packets.append(packet)
            elif self.player_id is not None and raw.packet_id == binding.SetHealthPacket.ID:
                self.player_state_packets.append(raw)
            elif raw.packet_id == binding.UpdateAbilitiesPacket.ID:
                # The target can only be checked once StartGame announces our unique ID.
                self.player_state_packets.append(raw)
            elif raw.packet_id == binding.AvailableCommandsPacket.ID:
                # A fresh advertisement supersedes the catalog and earlier enum updates.
                self.command_catalog_packets = [raw]
            elif raw.packet_id == binding.UpdateSoftEnumPacket.ID and self.command_catalog_packets:
                self.command_catalog_packets.append(raw)
            elif raw.packet_id == binding.SetCommandsEnabledPacket.ID:
                self.commands_enabled = bool(protocol.decode(raw).commands_enabled)
            elif raw.packet_id == binding.UpdateAttributesPacket.ID:
                packet = protocol.decode(raw)
                runtime_id = int(packet.runtime_id)
                if self.player_id is None or runtime_id == self.player_id:
                    speed = movement_speed_attribute(packet)
                    if speed is not None:
                        movement_speeds[runtime_id] = speed
                if runtime_id != self.player_id:
                    # Replay checks the target after StartGame establishes our runtime ID.
                    self.entity_packets.append(raw)
                elif any(
                    attribute.name in VITAL_ATTRIBUTES for attribute in packet.attribute_data
                ):
                    self.player_state_packets.append(raw)
            elif self.player_id is not None and raw.packet_id == binding.RespawnPacket.ID:
                packet = protocol.decode(raw)
                if packet.runtime_id in (0, self.player_id) and packet.state in (
                    binding.PlayerRespawnState.SEARCHING_FOR_SPAWN, binding.PlayerRespawnState.READY_TO_SPAWN,
                ):
                    self.player_state_packets.append(raw)
                    self.movement_packets.append(raw)
                    if packet.state == binding.PlayerRespawnState.READY_TO_SPAWN:
                        self.entity_packets.append(raw)
            elif raw.packet_id == binding.SetActorDataPacket.ID:
                packet = protocol.decode(raw)
                self.entity_packets.append(raw)
                if packet.id == self.player_id:
                    self.bed_packets.append(raw)
                    if air_metadata(packet, binding) or swimming_metadata(packet, binding) is not None:
                        self.player_state_packets.append(raw)
                    self.movement_packets.append(raw)
            elif raw.packet_id == binding.MobEffectPacket.ID:
                if self.player_id is not None and protocol.decode(raw).runtime_id == self.player_id:
                    self.player_state_packets.append(raw)
                else:
                    # Keep the effect in wire order with spawns, removals and dimension changes.
                    self.entity_packets.append(raw)
            elif raw.packet_id == binding.SetSpawnPositionPacket.ID:
                packet = protocol.decode(raw)
                if packet.spawn_pos_type == binding.SpawnPositionType.WORLD_SPAWN:
                    self.environment_packets.append(packet)
                elif self.player_id is not None and packet.spawn_pos_type == binding.SpawnPositionType.PLAYER_RESPAWN:
                    self.bed_packets.append(raw)
            elif self.player_id is not None and raw.packet_id == binding.TextPacket.ID:
                if invalidates_bed_respawn(protocol, raw):
                    self.bed_packets.append(raw)
            elif raw.packet_id == binding.BossEventPacket.ID:
                self.boss_bar_packets.append(protocol.decode(raw))
            elif raw.packet_id in FORM_PACKET_IDS:
                self.form_packets.append(protocol.decode(raw))
            elif raw.packet_id in SCOREBOARD_PACKET_IDS:
                self.scoreboard_packets.append(raw)
            elif raw.packet_id in (binding.InventoryContentPacket.ID, binding.InventorySlotPacket.ID,
                                   binding.PlayerHotbarPacket.ID):
                # Initial inventory can arrive before PlayStatus spawn; hand it
                # to Inventory in the original wire order after joining.
                self.inventory_packets.append(raw)
            elif raw.packet_id in (binding.MobEquipmentPacket.ID, binding.MobArmorEquipmentPacket.ID):
                runtime_id = int(protocol.decode(raw).runtime_id)
                if self.player_id is None:
                    early_equipment.append((raw, runtime_id, len(self.inventory_packets), len(self.entity_packets)))
                elif runtime_id != self.player_id:
                    self.entity_packets.append(raw)
                elif raw.packet_id == binding.MobEquipmentPacket.ID:
                    self.inventory_packets.append(raw)
            elif raw.packet_id in (
                binding.DimensionDataPacket.ID, binding.LevelChunkPacket.ID, binding.SubChunkPacket.ID,
                binding.NetworkChunkPublisherUpdatePacket.ID, binding.BlockActorDataPacket.ID,
                binding.UpdateBlockPacket.ID, binding.UpdateSubChunkBlocksPacket.ID,
            ):
                startup_chunks.append(raw)
            elif raw.packet_id in (
                binding.AddActorPacket.ID, binding.AddItemActorPacket.ID, binding.AddPaintingPacket.ID,
                binding.AddPlayerPacket.ID, binding.PlayerListPacket.ID, binding.PlayerSkinPacket.ID,
                binding.MovePlayerPacket.ID,
                binding.MoveActorAbsolutePacket.ID, binding.MoveActorDeltaPacket.ID,
                binding.RemoveActorPacket.ID, binding.TakeItemActorPacket.ID,
                binding.ChangeDimensionPacket.ID, binding.SetActorLinkPacket.ID,
            ):
                # Entities may be sent before PlayStatus spawn on reconnect.
                self.entity_packets.append(raw)
                if raw.packet_id == binding.ChangeDimensionPacket.ID and self.player_id is not None:
                    self.dimension_id = self._dimensions.active.dimension_id
                    self.movement_packets.append(raw)
                    startup_dimension_change = True
                    self.bed_packets.append(raw)
                    # Blocks starts in the final dimension. Keep this session's
                    # dimension definitions and discard the departed terrain.
                    startup_chunks[:] = [packet for packet in startup_chunks
                                         if packet.packet_id == binding.DimensionDataPacket.ID]
                elif (startup_dimension_change and raw.packet_id == binding.MovePlayerPacket.ID
                      and int(protocol.decode(raw).player_id) == self.player_id):
                    self.movement_packets.append(raw)
            elif startup_dimension_change and raw.packet_id == binding.ServerPlayerPostMovePositionPacket.ID:
                self.movement_packets.append(raw)
            elif raw.packet_id == binding.ItemRegistryPacket.ID:
                self._replace_item_registry(protocol.decode(raw))
                # Replay each registry at the point where startup drops saw it.
                self.entity_packets.append(raw)
            elif raw.packet_id == binding.AvailableActorIdentifiersPacket.ID:
                self._replace_entity_registry(raw)
            elif raw.packet_id == binding.ChunkRadiusUpdatedPacket.ID:
                self._on_chunk_radius(raw)
                self.chunk_radius = self.accepted_chunk_radius
            elif raw.packet_id == binding.StartGamePacket.ID:
                packs.check_ready()
                game = self.packets.protocol.decode(raw)
                self.player_id = int(game.runtime_id)
                self.player_unique_id = int(game.entity_id)
                self._enchantment_seed_at_join = getattr(game, "enchantment_seed", None)
                self.reported_server_version = getattr(game, "server_version", "") or None
                if self.commands_enabled is None:
                    self.commands_enabled = getattr(game.settings, "commands_enabled", None)
                mode = getattr(game, "entity_game_type", None)
                if mode is not None:
                    self.player_game_type = (game.settings.game_type.name
                                             if mode.name in ("DEFAULT", "UNDEFINED", "WORLD_DEFAULT")
                                             else mode.name)
                self.dimension_id = int(game.settings.spawn_settings.dimension)
                self.block_network_ids_are_hashes = bool(game.block_network_ids_are_hashes)
                self.vanilla_recipe_tags = (
                    protocol.number == 2225 and self.reported_server_version == "1.26.66"
                    and packs.plain_pack_info and stack_allows_vanilla_recipe_tags
                    and _experiments_allow_vanilla_recipe_tags(game.settings.experiments)
                )
                if protocol.number == 2225 and not self.block_network_ids_are_hashes:
                    # Vanilla also announces data-driven blocks. Use the static
                    # sequential palette only when the registry is confirmed.
                    self.has_custom_blocks = not matches_vanilla_palette(
                        game.server_block_type_registry_checksum, game.block_properties,
                    )
                else:
                    self.has_custom_blocks = bool(game.block_properties)
                self.environment_packets.append(game)
                self.player_state_packets.append(game)
                await self.request_chunk_radius(chunk_radius)
            elif raw.packet_id in environment_ids:
                self.environment_packets.append(protocol.decode(raw))
        # Resolve the early destinations once we know our runtime ID. Inserting
        # in reverse preserves wire order when packets share an insertion point.
        for raw, runtime_id, inventory_index, entity_index in reversed(early_equipment):
            if runtime_id != self.player_id:
                self.entity_packets.insert(entity_index, raw)
            elif raw.packet_id == binding.MobEquipmentPacket.ID:
                self.inventory_packets.insert(inventory_index, raw)
        self.movement_speed = movement_speeds.get(self.player_id)
        if retain_initial_chunks and startup_chunks:
            self.packets._replay_before_receiving(startup_chunks)
            self.replayed_dimension_packets = [raw for raw in startup_chunks
                                               if raw.packet_id == binding.DimensionDataPacket.ID]
        else:
            self.terrain_packets = startup_chunks
        self.resource_pack_index = packs.index

    def _replace_item_registry(self, packet):
        snapshot = ItemRegistrySnapshot({int(item.id): str(item.name) for item in packet.items})
        # Gameplay consumers borrow the same dictionary throughout this session.
        self.item_names.clear()
        self.item_names.update(snapshot.by_id)
        self.item_registry = snapshot

    def _on_item_registry(self, raw):
        protocol = self.packets.protocol
        if raw.packet_id == protocol.binding.ItemRegistryPacket.ID:
            self._replace_item_registry(protocol.decode(raw))
        elif raw.packet_id == protocol.binding.DisconnectPacket.ID:
            self._clear_item_registry()

    def _clear_item_registry(self, _cause=None):
        for unsubscribe in self._item_registry_subscriptions:
            unsubscribe()
        self._item_registry_subscriptions = ()
        self.item_registry = None
        self.item_names.clear()

    def _replace_entity_registry(self, raw: RawPacket) -> None:
        try:
            packet = self.packets.protocol.decode(raw)
        except DecodeError:
            return
        snapshot = _read_entity_registry(packet.identifier_list)
        if snapshot is not None:
            self.entity_registry = snapshot

    def _on_entity_registry(self, raw: RawPacket) -> None:
        binding = self.packets.protocol.binding
        if raw.packet_id == binding.AvailableActorIdentifiersPacket.ID:
            self._replace_entity_registry(raw)
        elif raw.packet_id == binding.DisconnectPacket.ID:
            self._clear_entity_registry()

    def _clear_entity_registry(self, _cause=None) -> None:
        for unsubscribe in self._entity_registry_subscriptions:
            unsubscribe()
        self._entity_registry_subscriptions = ()
        self.entity_registry = None

    def _on_chunk_radius(self, raw):
        protocol = self.packets.protocol
        if raw.packet_id == protocol.binding.ChunkRadiusUpdatedPacket.ID:
            self.accepted_chunk_radius = int(protocol.decode(raw).chunk_radius)

    def observe_chunk_radius(self):
        self._unsubscribe_chunk_radius = self.packets.subscribe_raw(self._on_chunk_radius)

    async def request_chunk_radius(self, radius: int) -> None:
        _validate_chunk_radius(radius)
        b = self.packets.protocol.binding
        await self.packets.send(b.RequestChunkRadiusPacket(chunk_radius=radius, max_chunk_radius=radius))
        self.requested_chunk_radius = radius

    async def wait_for_chunk_radius_update(self, timeout: float) -> int:
        protocol = self.packets.protocol
        b = protocol.binding
        raw = await self.packets.wait_for_packet((b.ChunkRadiusUpdatedPacket.ID, b.DisconnectPacket.ID),
                                                 timeout=timeout)
        if raw.packet_id == b.DisconnectPacket.ID:
            raise ConnectionError("server disconnected while waiting for a chunk radius update")
        return int(protocol.decode(raw).chunk_radius)

    async def wait_for_server_disconnect(self, timeout: float = 15):
        """Return an upstream DisconnectPacket, not a timeout or transport failure."""
        raw = await self.packets.wait_for_packet(
            self.packets.protocol.binding.DisconnectPacket.ID, timeout=timeout
        )
        return self.packets.protocol.decode(raw)

    def _observe_session_end(self) -> None:
        self._end_subscriptions = (
            self.packets.subscribe_raw(self._on_end_packet),
            self.packets.subscribe_reader_end(self._on_reader_end),
        )
        self._sync_session_end()

    def _stop_observing_session_end(self) -> None:
        subscriptions, self._end_subscriptions = self._end_subscriptions, ()
        for unsubscribe in subscriptions:
            unsubscribe()

    def _set_session_end_observer(self, callback: Callable[[SessionEnd], None]) -> None:
        # The Bot hands off the observer only after it publishes the joined session.
        result = self._end_state.result
        if result is None:
            self._end_observer = callback
        else:
            callback(result)

    def _finish_session_end(self, result: SessionEnd) -> None:
        if self._end_state.finish(result):
            self._stop_observing_session_end()
            observer, self._end_observer = self._end_observer, None
            if observer is not None:
                observer(result)

    def _on_end_packet(self, raw: RawPacket) -> None:
        protocol = self.packets.protocol
        disconnect = protocol.binding.DisconnectPacket
        if raw.packet_id != disconnect.ID or self._end_state.result is not None:
            return
        try:
            packet = protocol.decode(raw)
        except DecodeError:
            packet = None
        if not isinstance(packet, disconnect):
            self._finish_session_end(SessionEnd("server_disconnect"))
            return
        reason = packet.reason
        messages = packet.messages
        self._finish_session_end(SessionEnd(
            "server_disconnect",
            reason_code=int(reason),
            reason_name=getattr(reason, "name", None),
            message=None if messages is None else messages.message,
            filtered_message=None if messages is None else messages.filtered_message,
        ))

    def _on_reader_end(self, cause: BaseException | None) -> None:
        self._finish_session_end(SessionEnd("reader_end", error_type=None if cause is None else type(cause).__name__))

    def _sync_session_end(self) -> None:
        if self._end_state.result is None:
            ended, cause = self.packets._reader_end_result()
            if ended:
                self._on_reader_end(cause)

    def _on_local_close(self, reason: str | None) -> None:
        self._sync_session_end()
        self._finish_session_end(SessionEnd("local_close", message=reason))

    async def close(self) -> None:
        self._stop_observing_session_end()
        self._end_observer = None
        if isinstance(self.identity, _OnlineIdentity):
            self.identity = None
        self._clear_item_registry()
        self._clear_entity_registry()
        if self._unsubscribe_chunk_radius is not None:
            self._unsubscribe_chunk_radius()
            self._unsubscribe_chunk_radius = None
        try:
            await self.clock.close()
        finally:
            await self.packets.close()
