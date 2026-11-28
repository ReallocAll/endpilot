"""Published commands, player command feedback and command block edits."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from math import isfinite
import re
import secrets
import uuid

from bedrock_protocol import CommandBlockMode, DecodeError, TextPacketType, nbt

from .blocks import Blocks, BlockEntityUpdate, BlockStateObservation
from .protocol import RawPacket


_DISCONNECT_ERROR = "server disconnected while waiting for command feedback"
_OUTPUT_TIMEOUT_ERROR = "command output timed out"
_COMMAND_ORIGIN_SEQUENCE_MASK = (1 << 62) - 1
_COMMAND_ORIGIN_VARIANT = 2 << 62
_EDIT_DISCONNECT_ERROR = "connection ended during command block edit"
_EDIT_DIMENSION_ERROR = "dimension changed during command block edit"
_COMMAND_BLOCK_TYPES = {
    "impulse": ("minecraft:command_block", CommandBlockMode.NORMAL),
    "repeating": ("minecraft:repeating_command_block", CommandBlockMode.REPEATING),
    "chain": ("minecraft:chain_command_block", CommandBlockMode.CHAIN),
}
_COMMAND_BLOCK_MODES = dict(_COMMAND_BLOCK_TYPES.values())
_LITERAL_PLAYER_NAME = re.compile(r"[A-Za-z0-9_]+(?: +[A-Za-z0-9_]+)*")


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
class CommandCompletion:
    """Candidate tokens and the half-open Python string span they replace."""

    start: int
    end: int
    tokens: tuple[str, ...]


# A parameter holds hard enum values, an index into the live soft enums, or
# None for a player target that can be crossed using a plain literal name.
type _EnumRef = tuple[str, ...] | int
type _CompletionPrefix = tuple[_EnumRef | None, ...]


@dataclass(slots=True)
class _CommandAdvertisement:
    catalog: CommandCatalog
    overloads: dict[str, tuple[_CompletionPrefix, ...]]
    soft_enums: list[tuple[str, tuple[str, ...]]]
    player_target_commands: set[str]
    scoreboard_player_overloads: dict[str, list[_CompletionPrefix]]

    def enum_values(self, ref: _EnumRef) -> tuple[str, ...]:
        return self.soft_enums[ref][1] if isinstance(ref, int) else ref

    def matches_argument(self, ref: _EnumRef | None, argument: str) -> bool:
        if ref is None:
            return all(character.isalnum() or character == "_" for character in argument)
        return argument in self.enum_values(ref)

    def complete_quoted_scoreboard_target(
        self, name: str, text: str, player_names: Iterable[str],
    ) -> CommandCompletion | None:
        overloads = self.scoreboard_player_overloads.get(name)
        if not overloads:
            return None
        parts = text.split(maxsplit=3)
        if len(parts) != 4:
            return None
        _, category, action, target = parts
        if category != "players" or action not in ("set", "reset") or not target.startswith('"'):
            return None
        matched = [overload for overload in overloads
                   if self.matches_argument(overload[0], category) and self.matches_argument(overload[1], action)]
        if not matched:
            return None

        literal, closing_quote, remainder = target[1:].partition('"')
        end = len(text)
        if not closing_quote:
            return CommandCompletion(end - len(target), end, tuple(dict.fromkeys(_player_tokens(literal, player_names))))
        if not remainder or not remainder[0].isspace():
            return None
        prefix = remainder.lstrip()
        if any(character.isspace() for character in prefix):
            return None
        values = ()
        if _LITERAL_PLAYER_NAME.fullmatch(literal) and literal in player_names:
            values = tuple(dict.fromkeys(
                value for overload in matched if len(overload) > 3 and (ref := overload[3]) is not None
                for value in self.enum_values(ref) if value.startswith(prefix)
            ))
        return CommandCompletion(end - len(prefix), end, values)

    def update_soft_enum(self, packet, update_type) -> None:
        name, operation = packet.enum_name, packet.type
        values = packet.values
        for index, (enum_name, current) in enumerate(self.soft_enums):
            if enum_name != name:
                continue
            if operation == update_type.ADD:
                updated = tuple(dict.fromkeys((*current, *values)))
            elif operation == update_type.REMOVE:
                removed = set(values)
                updated = tuple(value for value in current if value not in removed)
            elif operation == update_type.REPLACE:
                updated = tuple(dict.fromkeys(values))
            else:
                return
            self.soft_enums[index] = (name, updated)


def _completion_prefix(params, hard_enums: tuple[tuple[str, ...], ...], soft_enum_count: int) -> _CompletionPrefix:
    result = []
    for param in params:
        symbol = param.parse_symbol
        # Command registry enum symbols use the low 20 bits for the index.
        kind, index = symbol & 0xFFF00000, symbol & 0xFFFFF
        if kind == 0x00300000 and index < len(hard_enums):
            result.append(hard_enums[index])
        elif kind == 0x04100000 and index < soft_enum_count:
            result.append(index)
        elif symbol == 0x0010000A and not param.optional:
            # The Target grammar also accepts selectors and quoted names.
            result.append(None)
        else:
            # Later parameters cannot be reached through an unsupported parameter.
            break
    return tuple(result)


def _command_advertisement(packet) -> _CommandAdvertisement:
    values = packet.enum_values
    hard_enums = tuple(
        tuple(dict.fromkeys(values[index] for index in enum.values if 0 <= index < len(values)))
        for enum in packet.enums
    )
    soft_enums = [(enum.name, tuple(dict.fromkeys(enum.values))) for enum in packet.soft_enums]
    commands = []
    overloads = {}
    player_target_commands = set()
    scoreboard_player_overloads = {}
    for record in packet.commands:
        alias_enum = record.alias_enum
        aliases = hard_enums[alias_enum] if 0 <= alias_enum < len(hard_enums) else ()
        commands.append(CommandCatalogEntry(record.name, aliases))
        # Protocol 2225's native tell uses this required first target parameter.
        if record.name == "tell" and any(
            not overload.is_chaining and overload.params
            and overload.params[0].parse_symbol == 0x00100008 and not overload.params[0].optional
            for overload in record.overloads
        ):
            player_target_commands.update({"tell", *aliases} & {"tell", "w", "msg"})
        prefixes = tuple(
            prefix for overload in record.overloads
            if not overload.is_chaining
            and (prefix := _completion_prefix(overload.params, hard_enums, len(soft_enums)))
        )
        if prefixes:
            scoreboard_targets = tuple(
                prefix for prefix in prefixes
                if record.name == "scoreboard" and len(prefix) >= 3 and prefix[2] is None
                and all(ref is not None for ref in prefix[:2])
            )
            for name in dict.fromkeys((record.name, *aliases)):
                if name:
                    overloads.setdefault(name, []).extend(prefixes)
                    if scoreboard_targets:
                        scoreboard_player_overloads.setdefault(name, []).extend(scoreboard_targets)
    return _CommandAdvertisement(
        CommandCatalog(tuple(commands)), {name: tuple(items) for name, items in overloads.items()}, soft_enums,
        player_target_commands, scoreboard_player_overloads,
    )


def _player_tokens(prefix: str, names: Iterable[str]) -> Iterable[str]:
    for name in names:
        if name.startswith(prefix) and _LITERAL_PLAYER_NAME.fullmatch(name):
            yield f'"{name}"' if " " in name else name


def _command_names(catalog: CommandCatalog, token: str) -> tuple[str, ...]:
    prefix = token.removeprefix("/")
    return tuple(dict.fromkeys(
        name for entry in catalog.commands for name in (entry.name, *entry.aliases)
        if name and name.startswith(prefix)
    ))


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
        initial_catalog_packets: Iterable[RawPacket] = (), initial_commands_enabled: bool | None = None,
        _on_chat_output: Callable | None = None,
    ):
        self.packets = packets
        self.player_uuid = player_uuid
        self.player_unique_id = player_unique_id
        self._on_chat_output = _on_chat_output
        # The high half identifies this connection and carries the UUIDv8 version bits.
        self._command_origin_prefix = (secrets.randbits(64) & ~(0xF << 12)) | (8 << 12)
        self._submitted_command_sequence = 0
        self._lock = asyncio.Lock()
        self._command_block_lock = asyncio.Lock()
        self._server_disconnected = False
        self._closed = False
        self._command_block_edits: set[asyncio.Future[Exception]] = set()
        self._advertisement: _CommandAdvertisement | None = None
        self._commands_enabled = initial_commands_enabled
        self._subscriptions = ()
        for raw in initial_catalog_packets:
            self._on_packet(raw)

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
        self._advertisement = None
        self._commands_enabled = None

    def _on_reader_end(self, _cause) -> None:
        self.close()

    def _end_command_block_edits(self, error_type: type[Exception], message: str) -> None:
        for failure in self._command_block_edits:
            if not failure.done():
                failure.set_result(error_type(message))

    @property
    def command_catalog(self) -> CommandCatalog | None:
        return None if self._advertisement is None else self._advertisement.catalog

    @property
    def commands_enabled(self) -> bool | None:
        return self._commands_enabled

    def complete_name(self, prefix: str) -> tuple[str, ...] | None:
        """Match advertised names and aliases in their original spelling."""
        catalog = self.command_catalog
        if catalog is None:
            return None
        if any(character.isspace() for character in prefix):
            raise ValueError("command-name prefix must not contain whitespace")
        return _command_names(catalog, prefix)

    def complete_command(self, text: str) -> tuple[str, ...] | None:
        """Match the current name or advertised enum argument at the end of text."""
        completion = self.complete_command_with_span(text)
        return None if completion is None else completion.tokens

    def complete_command_with_span(self, text: str, player_names: Iterable[str] = ()) -> CommandCompletion | None:
        """Match at the end of text using the current advertisement and player names."""
        advertisement = self._advertisement
        if advertisement is None:
            return None
        end = len(text)
        tokens = text.split()
        if not tokens:
            return CommandCompletion(end, end, _command_names(advertisement.catalog, ""))
        if not text[-1].isspace():
            if len(tokens) == 1:
                token = tokens[0]
                start = end - len(token.removeprefix("/"))
                return CommandCompletion(start, end, _command_names(advertisement.catalog, token))
            prefix = tokens.pop()
        else:
            prefix = ""
        command, *arguments = tokens
        name = command.removeprefix("/")
        if (quoted := advertisement.complete_quoted_scoreboard_target(name, text, player_names)) is not None:
            return quoted
        start = end - len(prefix)
        position = len(arguments)
        matches = []
        for overload in advertisement.overloads.get(name, ()):
            if position >= len(overload):
                continue
            ref = overload[position]
            if ref is not None and all(
                advertisement.matches_argument(previous, argument)
                for previous, argument in zip(overload, arguments)
            ):
                matches.extend(value for value in advertisement.enum_values(ref) if value.startswith(prefix))
        if name in advertisement.player_target_commands:
            target = text.lstrip()[len(command):].lstrip(" \t")
            if target.startswith('"') and '"' not in target[1:]:
                target_start = end - len(target)
                if not matches or target_start == start:
                    start = target_start
                    matches.extend(_player_tokens(target[1:], player_names))
            elif position == 0 and target == prefix:
                matches.extend(_player_tokens(prefix, player_names))
        return CommandCompletion(start, end, tuple(dict.fromkeys(matches)))

    def _on_packet(self, raw: RawPacket) -> None:
        protocol = self.packets.protocol
        binding = protocol.binding
        if raw.packet_id == binding.AvailableCommandsPacket.ID:
            self._advertisement = _command_advertisement(protocol.decode(raw))
        elif raw.packet_id == binding.UpdateSoftEnumPacket.ID:
            if self._advertisement is not None:
                self._advertisement.update_soft_enum(protocol.decode(raw), binding.SoftEnumUpdateType)
        elif raw.packet_id == binding.SetCommandsEnabledPacket.ID:
            self._commands_enabled = bool(protocol.decode(raw).commands_enabled)
        elif raw.packet_id == binding.DisconnectPacket.ID:
            self._server_disconnected = True
            self.close()
        elif raw.packet_id == binding.ChangeDimensionPacket.ID:
            self._end_command_block_edits(RuntimeError, _EDIT_DIMENSION_ERROR)
        elif (self._on_chat_output is not None and protocol.number == 2225
              and raw.packet_id == binding.CommandOutputPacket.ID):
            try:
                response = protocol.decode(raw)
            except DecodeError:
                return  # A raw command waiter handles its own decode errors.
            origin = response.origin_data
            if origin.type != binding.CommandOriginType.PLAYER:
                return
            if origin.uuid != self.player_uuid and not self._owns_command_origin(origin.uuid):
                return
            self._on_chat_output(response)

    def _command_origin_uuid(self, sequence: int) -> uuid.UUID:
        return uuid.UUID(int=(self._command_origin_prefix << 64) | _COMMAND_ORIGIN_VARIANT | sequence)

    def _owns_command_origin(self, origin: uuid.UUID) -> bool:
        value = origin.int
        sequence = value & _COMMAND_ORIGIN_SEQUENCE_MASK
        return (
            value >> 64 == self._command_origin_prefix
            and ((value >> 62) & 3) == 2
            and 0 < sequence <= self._submitted_command_sequence
        )

    def _request(self, command: str, *, origin_uuid: uuid.UUID | None = None):
        b = self.packets.protocol.binding
        origin = b.CommandOriginData(
            type=b.CommandOriginType.PLAYER,
            uuid=self.player_uuid if origin_uuid is None else origin_uuid,
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

        def matches(packet):
            body = packet.body
            return isinstance(body, text.MessageOnly) and body.type == TextPacketType.TEXT_OBJECT

        packet = self._request(command)
        async with self._lock:
            return await self._request_feedback(packet, text.ID, matches, timeout=timeout)

    async def execute_output(self, command: str, *, timeout: float = 15):
        if not isfinite(timeout):
            raise ValueError("command output timeout must be finite")
        if timeout <= 0:
            raise TimeoutError(_OUTPUT_TIMEOUT_ERROR)

        deadline = asyncio.get_running_loop().time() + timeout
        binding = self.packets.protocol.binding
        if not command.startswith("/"):
            command = f"/{command}"
        async with asyncio.timeout_at(deadline), self._lock:
            sequence = self._submitted_command_sequence + 1
            if sequence > _COMMAND_ORIGIN_SEQUENCE_MASK:
                raise OverflowError("command origin UUID sequence exhausted")
            origin_uuid = self._command_origin_uuid(sequence)
            packet = self._request(command, origin_uuid=origin_uuid)

            def matches(response):
                # BDS echoes this UUID and can clear the request ID and player ID.
                origin = response.origin_data
                return (self._submitted_command_sequence == sequence
                        and origin.type == binding.CommandOriginType.PLAYER and origin.uuid == origin_uuid)

            def submit():
                if self._closed:
                    raise ConnectionError(_DISCONNECT_ERROR)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(_OUTPUT_TIMEOUT_ERROR)
                self._submitted_command_sequence = sequence

            return await self._request_feedback(
                packet, binding.CommandOutputPacket.ID, matches, timeout=None, before_send=submit,
            )

    async def _request_feedback(self, packet, packet_id, matches, *, timeout, before_send=None):
        protocol = self.packets.protocol
        disconnect = protocol.binding.DisconnectPacket.ID
        feedback = None

        def observe(raw):
            nonlocal feedback
            if raw.packet_id == disconnect:
                return True
            response = protocol.decode(raw)
            if matches(response):
                feedback = response
                return True
            return False

        if self._server_disconnected:
            raise ConnectionError(_DISCONNECT_ERROR)
        # Arm before send: a quick server response must not be lost.
        waiting = asyncio.create_task(self.packets.wait_for_packet(
            (packet_id, disconnect), match=observe, timeout=timeout,
        ))
        tasks = [waiting]
        try:
            await asyncio.sleep(0)
            if self._server_disconnected:
                raise ConnectionError(_DISCONNECT_ERROR)
            if before_send is None:
                await self.packets.send(packet)
            else:
                sending = asyncio.create_task(self.packets.send(packet, _before_send=before_send))
                tasks.append(sending)
                # A disconnect or reader error can arrive while the send is blocked.
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if waiting not in done and not self.packets._reader_end_result()[0]:
                    sending.result()
            response = await waiting
            if response.packet_id == disconnect:
                raise ConnectionError(_DISCONNECT_ERROR)
            if before_send is not None:
                await sending
            return feedback
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def set_command_block(
        self, blocks: Blocks, position: tuple[int, int, int], command: str, *,
        timeout: float, mode: str | None = None, always_active: bool | None = None,
        conditional: bool | None = None, track_output: bool | None = None,
    ) -> BlockEntityUpdate:
        if (not isinstance(position, (tuple, list)) or len(position) != 3
                or any(type(axis) is not int or not -(1 << 31) <= axis < (1 << 31) for axis in position)):
            raise ValueError("command block position must contain three signed 32-bit integers")
        position = tuple(position)
        if not isinstance(command, str):
            raise ValueError("command block command must be a string")
        if mode is not None and (type(mode) is not str or mode not in _COMMAND_BLOCK_TYPES):
            raise ValueError("command block mode must be 'impulse', 'repeating', or 'chain'")
        for option, value in (
            ("always_active", always_active), ("conditional", conditional), ("track_output", track_output),
        ):
            if value is not None and type(value) is not bool:
                raise ValueError(f"command block {option} must be a bool")
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
        target_seen = False

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
        if mode is None:
            target_name = initial_block.name
            target_mode = _COMMAND_BLOCK_MODES[target_name]
        else:
            target_name, target_mode = _COMMAND_BLOCK_TYPES[mode]
        target_auto = not options.requires_redstone if always_active is None else always_active
        target_conditional = options.conditional if conditional is None else conditional
        target_track_output = options.track_output if track_output is None else track_output
        expected_flags = ()
        if any(option is not None for option in (mode, always_active, conditional, track_output)):
            expected_flags = (
                ("auto", target_auto), ("conditionalMode", target_conditional), ("TrackOutput", target_track_output),
            )

        b = self.packets.protocol.binding
        update_type = b.CommandBlockUpdatePacket
        x, y, z = position
        target = update_type.BlockCommandData(
            block_pos=b.BlockPos(x=x, y=y, z=z), mode=target_mode,
            redstone_mode=not target_auto, is_conditional=target_conditional,
        )
        packet = update_type(
            target=target, command=command, last_output=options.last_output,
            name=options.name, filtered_name=options.filtered_name, track_output=target_track_output,
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
            if (not isinstance(actor_id, nbt.StringTag) or actor_id.value != "CommandBlock"
                    or not isinstance(text, nbt.StringTag) or text.value != command):
                return
            block = blocks.observed_block_state(position)
            if block is None or block.name != target_name:
                return
            for key, value in expected_flags:
                if key not in root or not isinstance(tag := root[key], nbt.ByteTag) or tag.value != value:
                    return
            if conditional is not None and block.properties.get("conditional_bit") != target_conditional:
                return
            received.set_result(update)

        terrain_packets = (
            b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID, b.SubChunkPacket.ID,
            b.LevelChunkPacket.ID, b.NetworkChunkPublisherUpdatePacket.ID,
        )

        def observe_terrain(raw: RawPacket):
            nonlocal target_seen
            if failure.done() or raw.packet_id not in terrain_packets:
                return
            if send_started:
                block = blocks.observed_block_state(position)
                if block is None:
                    return
                if block.name == target_name:
                    target_seen = True
                elif target_seen or block.name != initial_block.name:
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
