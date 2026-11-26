"""Published commands, player command feedback and command block edits."""

import asyncio
from dataclasses import dataclass, field
from math import isfinite
import uuid

from bedrock_protocol import CommandBlockMode, TextPacketType, nbt

from .blocks import Blocks, BlockEntityUpdate, BlockStateObservation
from .protocol import RawPacket


_DISCONNECT_ERROR = "server disconnected while waiting for command feedback"
_EDIT_DISCONNECT_ERROR = "connection ended during command block edit"
_EDIT_DIMENSION_ERROR = "dimension changed during command block edit"
_COMMAND_BLOCK_MODES = {
    "minecraft:command_block": CommandBlockMode.NORMAL,
    "minecraft:repeating_command_block": CommandBlockMode.REPEATING,
    "minecraft:chain_command_block": CommandBlockMode.CHAIN,
}


@dataclass(frozen=True, slots=True)
class CommandCatalogEntry:
    """A published name and the aliases referenced by its advertisement."""

    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCatalog:
    """An independent snapshot of one server command advertisement."""

    commands: tuple[CommandCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class _CommandBlockOptions:
    requires_redstone: bool
    conditional: bool
    name: str
    filtered_name: str
    # Output can change while the edit waits without changing its settings.
    last_output: str = field(compare=False)
    track_output: bool
    tick_delay: int
    execute_on_first_tick: bool


def _command_block_snapshot(
    blocks: Blocks, position: tuple[int, int, int],
) -> tuple[BlockStateObservation, _CommandBlockOptions]:
    block = blocks.observed_block_state(position)
    if block is None or block.name not in _COMMAND_BLOCK_MODES:
        raise RuntimeError("a supported command block must be observed at the target position")
    root = blocks.observed_block_entity(position)
    actor_id = root["id"] if root is not None and "id" in root else None
    if not isinstance(actor_id, nbt.StringTag) or actor_id.value != "CommandBlock":
        raise RuntimeError("CommandBlock NBT must be observed at the target position")
    try:
        auto, conditional, track_output, first_tick = (
            root[key] for key in ("auto", "conditionalMode", "TrackOutput", "ExecuteOnFirstTick")
        )
        name, last_output, delay = root["CustomName"], root["LastOutput"], root["TickDelay"]
    except KeyError:
        raise RuntimeError("command block configuration is missing or invalid") from None
    filtered_name = root["FilteredCustomName"] if "FilteredCustomName" in root else None
    if (any(not isinstance(tag, nbt.ByteTag) or tag.value not in (0, 1)
            for tag in (auto, conditional, track_output, first_tick))
            or not isinstance(name, nbt.StringTag)
            or (filtered_name is not None and not isinstance(filtered_name, nbt.StringTag))
            or not isinstance(last_output, nbt.StringTag)
            or not isinstance(delay, nbt.IntTag) or delay.value < 0):
        raise RuntimeError("command block configuration is missing or invalid")
    return block, _CommandBlockOptions(
        not auto.value, bool(conditional.value), name.value,
        "" if filtered_name is None else filtered_name.value, last_output.value,
        bool(track_output.value), delay.value, bool(first_tick.value),
    )


class Commands:
    def __init__(
        self, packets, player_uuid: uuid.UUID, player_unique_id: int, *,
        initial_catalog: RawPacket | None = None, initial_commands_enabled: bool | None = None,
    ):
        self.packets = packets
        self.player_uuid = player_uuid
        self.player_unique_id = player_unique_id
        self._lock = asyncio.Lock()
        self._command_block_lock = asyncio.Lock()
        self._server_disconnected = False
        self._closed = False
        self._command_block_edits: set[asyncio.Future[Exception]] = set()
        self._catalog: CommandCatalog | None = None
        self._commands_enabled = initial_commands_enabled
        self._subscriptions = ()
        if initial_catalog is not None:
            self._on_packet(initial_catalog)

    def start(self) -> None:
        if not self._subscriptions:
            self._subscriptions = (
                self.packets.subscribe_raw(self._on_packet),
                self.packets.subscribe_reader_end(self._on_reader_end),
            )

    def close(self) -> None:
        self._closed = True
        self._end_command_block_edits(ConnectionError, _EDIT_DISCONNECT_ERROR)
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions = ()
        self._catalog = None
        self._commands_enabled = None

    def _on_reader_end(self, _cause) -> None:
        self.close()

    def _end_command_block_edits(self, error_type: type[Exception], message: str) -> None:
        for failure in self._command_block_edits:
            if not failure.done():
                failure.set_result(error_type(message))

    @property
    def command_catalog(self) -> CommandCatalog | None:
        return self._catalog

    @property
    def commands_enabled(self) -> bool | None:
        return self._commands_enabled

    def complete_name(self, prefix: str) -> tuple[str, ...] | None:
        """Match advertised names and aliases in their original spelling."""
        catalog = self._catalog
        if catalog is None:
            return None
        if any(character.isspace() for character in prefix):
            raise ValueError("command-name prefix must not contain whitespace")
        token = prefix.removeprefix("/")
        return tuple(dict.fromkeys(
            name for entry in catalog.commands for name in (entry.name, *entry.aliases)
            if name and name.startswith(token)
        ))

    def _on_packet(self, raw: RawPacket) -> None:
        protocol = self.packets.protocol
        binding = protocol.binding
        if raw.packet_id == binding.AvailableCommandsPacket.ID:
            packet = protocol.decode(raw)
            values, enums = packet.enum_values, packet.enums
            commands = []
            for record in packet.commands:
                aliases = ()
                alias_enum = record.alias_enum
                if 0 <= alias_enum < len(enums):
                    # The global string table also includes unrelated argument values.
                    aliases = tuple(dict.fromkeys(values[index] for index in enums[alias_enum].values
                                                  if 0 <= index < len(values)))
                commands.append(CommandCatalogEntry(record.name, aliases))
            self._catalog = CommandCatalog(tuple(commands))
        elif raw.packet_id == binding.SetCommandsEnabledPacket.ID:
            self._commands_enabled = bool(protocol.decode(raw).commands_enabled)
        elif raw.packet_id == binding.DisconnectPacket.ID:
            self._server_disconnected = True
            self.close()
        elif raw.packet_id == binding.ChangeDimensionPacket.ID:
            self._end_command_block_edits(RuntimeError, _EDIT_DIMENSION_ERROR)

    def _request(self, command: str):
        b = self.packets.protocol.binding
        origin = b.CommandOriginData(
            type=b.CommandOriginType.PLAYER,
            uuid=self.player_uuid,
            request_id=uuid.uuid4().hex,
            player_id=self.player_unique_id,
        )
        return b.CommandRequestPacket(
            command=command, origin=origin,
            internal_source=False, version=b.CurrentCmdVersion.LATEST,
        )

    async def send(self, command: str) -> None:
        if not command.startswith("/"):
            command = f"/{command}"
        async with self._lock:
            await self.packets.send(self._request(command))

    async def execute(self, command: str, *, timeout: float = 15):
        """Submit a command and return the next typed BDS command-text response.

        BDS 1.26.52 sends TextPacket(TEXT_OBJECT) without a request ID, not a
        CommandOutputPacket. Commands from this player are serialized, but other
        server text can still interleave: inspect the feedback and server effect.
        """
        binding = self.packets.protocol.binding
        text = binding.TextPacket
        disconnect = binding.DisconnectPacket.ID
        packet = self._request(command)

        def matches(raw):
            if raw.packet_id == disconnect:
                return True
            body = self.packets.protocol.decode(raw).body
            return isinstance(body, text.MessageOnly) and body.type == TextPacketType.TEXT_OBJECT

        async with self._lock:
            if self._server_disconnected:
                raise ConnectionError(_DISCONNECT_ERROR)
            # Arm before send: a quick server response must not be lost.
            waiting = asyncio.create_task(self.packets.wait_for_packet(
                (text.ID, disconnect), match=matches, timeout=timeout,
            ))
            try:
                await asyncio.sleep(0)
                if self._server_disconnected:
                    raise ConnectionError(_DISCONNECT_ERROR)
                await self.packets.send(packet)
                response = await waiting
                if response.packet_id == disconnect:
                    raise ConnectionError(_DISCONNECT_ERROR)
                return self.packets.protocol.decode(response)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def set_command_block(self, blocks: Blocks, position: tuple[int, int, int], command: str,
                                *, timeout: float) -> BlockEntityUpdate:
        if (not isinstance(position, (tuple, list)) or len(position) != 3
                or any(type(axis) is not int or not -(1 << 31) <= axis < (1 << 31) for axis in position)):
            raise ValueError("command block position must contain three signed 32-bit integers")
        position = tuple(position)
        if not isinstance(command, str):
            raise ValueError("command block command must be a string")
        if not isfinite(timeout):
            raise ValueError("command block edit timeout must be finite")
        if timeout <= 0:
            raise TimeoutError("command block edit timed out")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        movement = blocks.movement
        dimension, epoch = blocks.dimension_id, movement._dimension_epoch
        failure: asyncio.Future[Exception] = loop.create_future()
        received: asyncio.Future[BlockEntityUpdate] = loop.create_future()
        send_started = False

        def require_context():
            if failure.done():
                raise failure.result()
            if self._closed or self.packets._reader_end_result()[0]:
                raise ConnectionError(_EDIT_DISCONNECT_ERROR)
            if movement._changing_dimension or movement._dimension_epoch != epoch or blocks.dimension_id != dimension:
                raise RuntimeError(_EDIT_DIMENSION_ERROR)
            if loop.time() >= deadline:
                raise TimeoutError("command block edit timed out")

        require_context()
        command.encode("utf-8")
        snapshot = _command_block_snapshot(blocks, position)
        initial_block, options = snapshot
        b = self.packets.protocol.binding
        update_type = b.CommandBlockUpdatePacket
        x, y, z = position
        target = update_type.BlockCommandData(
            block_pos=b.BlockPos(x=x, y=y, z=z), mode=_COMMAND_BLOCK_MODES[initial_block.name],
            redstone_mode=options.requires_redstone, is_conditional=options.conditional,
        )
        packet = update_type(
            target=target, command=command, last_output=options.last_output,
            name=options.name, filtered_name=options.filtered_name, track_output=options.track_output,
            tick_delay=options.tick_delay, execute_on_first_tick=options.execute_on_first_tick,
        )

        def observe(update: BlockEntityUpdate):
            if (not send_started or received.done() or failure.done() or movement._changing_dimension
                    or movement._dimension_epoch != epoch):
                return
            root = update.data
            if "id" not in root or "Command" not in root:
                return
            actor_id, text = root["id"], root["Command"]
            if (isinstance(actor_id, nbt.StringTag) and actor_id.value == "CommandBlock"
                    and isinstance(text, nbt.StringTag) and text.value == command):
                block = blocks.observed_block_state(position)
                if block is not None and block.name == initial_block.name:
                    received.set_result(update)

        terrain_packets = (
            b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID, b.SubChunkPacket.ID,
            b.LevelChunkPacket.ID, b.NetworkChunkPublisherUpdatePacket.ID,
        )

        def observe_terrain(raw: RawPacket):
            if failure.done() or raw.packet_id not in terrain_packets:
                return
            if send_started:
                block = blocks.observed_block_state(position)
                if block is not None and block.name != initial_block.name:
                    failure.set_result(RuntimeError("command block target changed while waiting for its update"))
                return
            # A replacement can clear the actor before a later packet describes a new one.
            current = blocks.observed_block(position)
            if (current is None or current.runtime_id != initial_block.runtime_id
                    or blocks._block_entity(position) is None):
                failure.set_result(RuntimeError("command block observation was invalidated before sending"))

        def before_send():
            nonlocal send_started
            require_context()
            if _command_block_snapshot(blocks, position) != snapshot:
                raise RuntimeError("command block or its configuration changed before sending")
            require_context()
            send_started = True

        async def edit():
            async with self._command_block_lock:
                require_context()
                await self.packets.send(packet, _before_send=before_send)
                return await received

        stop_updates = blocks.subscribe_block_entity_updates(observe, position=position, dimension_id=dimension)
        stop_terrain = self.packets.subscribe_raw(observe_terrain)
        self._command_block_edits.add(failure)
        editing = asyncio.create_task(edit())
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.wait((editing, failure), return_when=asyncio.FIRST_COMPLETED)
                require_context()
                return editing.result()
        finally:
            stop_updates()
            stop_terrain()
            self._command_block_edits.discard(failure)
            failure.cancel()
            received.cancel()
            if not editing.done():
                editing.cancel()
                await asyncio.gather(editing, return_exceptions=True)
            elif not editing.cancelled():
                editing.exception()
