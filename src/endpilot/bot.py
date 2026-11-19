"""Public player entry point; game state delegates to the relevant feature."""

from .authentication import OfflineIdentity
from .blocks import Blocks, BlockUpdate
from .inventory import Inventory
from .login import JoinSession
from .movement import Movement
from .protocol import Protocol


class Bot:
    def __init__(self, name: str, protocol: int = 2193):
        self.identity = OfflineIdentity(name)
        self.protocol = Protocol(protocol)
        self._session: JoinSession | None = None
        self._movement: Movement | None = None
        self._inventory: Inventory | None = None
        self._blocks: Blocks | None = None

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
        self._blocks = Blocks(session.packets, self._movement, self._inventory)
        self._session = session
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

    async def walk_forward(self, ticks: int = 24) -> int:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward(ticks)

    async def wait_for_block_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_update(position, layer=layer, timeout=timeout)

    async def place_against(self, against: BlockUpdate, *, face: int = 1, slot: int = 0) -> int:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.place_against(against, face=face, slot=slot)

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
        self._blocks = None
        if movement is not None:
            movement.close()
        if inventory is not None:
            inventory.close()
        if session is not None:
            await session.close()
