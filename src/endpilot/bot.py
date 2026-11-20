"""Public player entry point; game state delegates to the relevant feature."""

from .authentication import OfflineIdentity
from .blocks import Blocks, BlockObservation, BlockUpdate
from .chat import Chat, ChatMessage
from .commands import Commands
from .entities import Entities, EntityMovement, EntitySpawn, EntityState
from .inventory import Inventory
from .login import JoinSession
from .movement import Movement
from .player_state import PlayerState
from .protocol import Protocol


class Bot:
    def __init__(self, name: str, protocol: int = 2193):
        self.identity = OfflineIdentity(name)
        self.protocol = Protocol(protocol)
        self._session: JoinSession | None = None
        self._movement: Movement | None = None
        self._inventory: Inventory | None = None
        self._blocks: Blocks | None = None
        self._chat: Chat | None = None
        self._commands: Commands | None = None
        self._entities: Entities | None = None
        self._player_state: PlayerState | None = None

    async def join_nethernet(self, url: str, *, timeout: float = 30, server_key_sha256: str | None = None) -> None:
        if self._session is not None:
            raise RuntimeError("player is already connected")
        session = await JoinSession.connect_nethernet(
            url, self.identity, self.protocol, timeout, server_key_sha256=server_key_sha256
        )
        self._start_session(session)

    async def join_raknet(self, host: str, port: int = 19132, *, timeout: float = 30) -> None:
        if self._session is not None:
            raise RuntimeError("player is already connected")
        session = await JoinSession.connect_raknet(host, port, self.identity, self.protocol, timeout)
        self._start_session(session)

    def _start_session(self, session: JoinSession) -> None:
        self._movement = Movement(session.packets, session.player_id)
        self._inventory = Inventory(session.packets, session.player_id)
        self._blocks = Blocks(
            session.packets, self._movement, self._inventory,
            hashed_runtime_ids=session.block_network_ids_are_hashes,
        )
        self._chat = Chat(session.packets, self.identity.name)
        self._commands = Commands(session.packets, self.identity.uuid, session.player_unique_id)
        self._entities = Entities(session.packets, item_names=session.item_names)
        self._player_state = PlayerState(session.packets, session.player_id)
        self._session = session
        self._entities.start()
        self._blocks.start()
        self._movement.start()

    @property
    def player_id(self) -> int:
        if self._session is None or self._session.player_id is None:
            raise RuntimeError("player has not joined")
        return self._session.player_id

    @property
    def position(self) -> tuple[float, float, float] | None:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.position

    async def wait_for_position_sync(self, timeout: float = 15) -> tuple[float, float, float]:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.wait_for_position_sync(timeout)

    async def look_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        """Aim toward a world-space eye-height target on subsequent input frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.look_at(target)

    async def walk_forward(self, ticks: int = 24) -> int:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward(ticks)

    async def stop_moving(self) -> None:
        """Stop an active walk/follow, waiting for its input to be released."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.stop_moving()

    async def follow_entity(self, runtime_id: int, *, ticks: int = 80,
                            stop_distance: float = 1.5) -> int:
        """Follow the latest server-observed actor position for a bounded time."""
        if self._movement is None or self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._movement.follow_entity(
            self._entities, runtime_id, ticks=ticks, stop_distance=stop_distance
        )

    async def wait_for_block_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_update(position, layer=layer, timeout=timeout)

    def observed_block(
        self, position: tuple[int, int, int], *, layer: int = 0
    ) -> BlockObservation | None:
        """Last server-supplied block, or None until that position is known."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_block(position, layer=layer)

    async def request_sub_chunk(self, position: tuple[int, int, int]) -> None:
        """Ask BDS for the containing section; inspect observed_block afterwards."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        await self._blocks.request_sub_chunk(position)

    async def place_against(self, against: BlockUpdate, *, face: int = 1, slot: int = 0) -> int:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.place_against(against, face=face, slot=slot)

    async def send_chat(self, message: str) -> None:
        """Submit a chat packet; server delivery is not implied."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        await self._chat.send(message)

    async def wait_for_chat(self, *, author: str | None = None, timeout: float = 15) -> ChatMessage:
        """Wait for a new player chat relayed by the server."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_message(author=author, timeout=timeout)

    async def execute_command(self, command: str, *, timeout: float = 15):
        """Submit a player command and observe the next command-style server text.

        The text has no request ID and does not prove the command took effect.
        """
        if self._commands is None:
            raise RuntimeError("player has not joined")
        return await self._commands.execute(command, timeout=timeout)

    @property
    def health(self) -> float | None:
        """Latest server-reported health; None until a health packet arrives."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.health

    async def wait_for_health_update(self, timeout: float = 15) -> float:
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_health_update(timeout)

    @property
    def observed_entities(self) -> tuple[EntityState, ...]:
        """Last observed AddActor states, not a complete scan of loaded entities."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.snapshot()

    @property
    def observed_drops(self) -> tuple[EntityState, ...]:
        """Dropped items visible on this player stream, not all world drops."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.drops()

    async def wait_for_item_drop(self, timeout: float = 15) -> EntityState:
        """Observe the next item actor spawn sent by the server."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_item_drop(timeout)

    async def wait_for_entity_spawn(
        self, *, actor_type: str | None = None, timeout: float = 15
    ) -> EntitySpawn:
        """Observe the next server actor spawn; not a scan of all world entities."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_spawn(actor_type=actor_type, timeout=timeout)

    async def wait_for_entity_removal(self, entity_id: int, *, timeout: float = 15) -> None:
        """Observe the server removing the actor with this unique ID from view."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.wait_for_removal(entity_id, timeout=timeout)

    async def wait_for_entity_movement(
        self, runtime_id: int, *, timeout: float = 15
    ) -> EntityMovement:
        """Observe one actor movement event, not a continuously tracked position."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_movement(runtime_id, timeout=timeout)

    async def wait_for_server_disconnect(self, timeout: float = 15):
        if self._session is None:
            raise RuntimeError("player has not joined")
        return await self._session.wait_for_server_disconnect(timeout)

    @property
    def packets(self):
        """Expose typed and raw packet I/O for external extensions."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.packets

    async def close(self) -> None:
        session, self._session = self._session, None
        movement, self._movement = self._movement, None
        inventory, self._inventory = self._inventory, None
        blocks, self._blocks = self._blocks, None
        if blocks is not None:
            blocks.close()
        self._chat = None
        self._commands = None
        entities, self._entities = self._entities, None
        player_state, self._player_state = self._player_state, None
        if player_state is not None:
            player_state.close()
        if entities is not None:
            entities.close()
        if movement is not None:
            movement.close()
        if inventory is not None:
            inventory.close()
        if session is not None:
            await session.close()
