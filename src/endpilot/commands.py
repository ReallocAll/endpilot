"""Published command names, player-initiated commands and BDS text feedback."""

import asyncio
from dataclasses import dataclass
import uuid

from bedrock_protocol import TextPacketType

from .protocol import RawPacket


@dataclass(frozen=True, slots=True)
class CommandCatalogEntry:
    """A published name and the aliases referenced by its advertisement."""

    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCatalog:
    """An independent snapshot of one server command advertisement."""

    commands: tuple[CommandCatalogEntry, ...]


class Commands:
    def __init__(
        self, packets, player_uuid: uuid.UUID, player_unique_id: int, *,
        initial_catalog: RawPacket | None = None, initial_commands_enabled: bool | None = None,
    ):
        self.packets = packets
        self.player_uuid = player_uuid
        self.player_unique_id = player_unique_id
        self._lock = asyncio.Lock()
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
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions = ()
        self._catalog = None
        self._commands_enabled = None

    def _on_reader_end(self, _cause) -> None:
        self.close()

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
            self.close()

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
        text = self.packets.protocol.binding.TextPacket
        packet = self._request(command)

        def matches(raw):
            body = self.packets.protocol.decode(raw).body
            return isinstance(body, text.MessageOnly) and body.type == TextPacketType.TEXT_OBJECT

        async with self._lock:
            # Arm before send: a quick server response must not be lost.
            waiting = asyncio.create_task(self.packets.wait_for_packet(
                text.ID, match=matches, timeout=timeout,
            ))
            await asyncio.sleep(0)
            try:
                await self.packets.send(packet)
                return self.packets.protocol.decode(await waiting)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
