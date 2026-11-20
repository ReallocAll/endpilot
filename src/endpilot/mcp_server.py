"""Stdio MCP tools over the same player actions used by ordinary Python scripts."""

import asyncio

from .blocks import BlockUpdate
from .bot import Bot


class MCPPlayer:
    def __init__(self):
        self.bot: Bot | None = None
        self._pending: dict[tuple[tuple[int, int, int], int], asyncio.Task[BlockUpdate]] = {}
        self._observed: dict[tuple[tuple[int, int, int], int], BlockUpdate] = {}

    def _joined(self) -> Bot:
        if self.bot is None:
            raise RuntimeError("connect a player first")
        return self.bot

    async def connect(self, url: str, name: str = "EndPilotTest") -> dict:
        if self.bot is not None:
            raise RuntimeError("a player is already connected")
        bot = Bot(name)
        try:
            if url.startswith("raknet://"):
                host, separator, port = url.removeprefix("raknet://").rpartition(":")
                if not separator:
                    host, port = port, "19132"
                await bot.join_raknet(host, int(port))
            else:
                await bot.join_nethernet(url)
        except BaseException:
            await bot.close()
            raise
        self.bot = bot
        return {"name": name, "player_id": bot.player_id}

    async def watch_block(self, x: int, y: int, z: int, layer: int = 0, timeout: float = 15) -> dict:
        """Arm a server UpdateBlock watcher before causing a world change."""
        bot = self._joined()
        position = (x, y, z)
        key = (position, layer)
        previous = self._pending.get(key)
        if previous is not None:
            # Preserve unread updates, but allow retry once a watcher has failed.
            if not previous.done() or (not previous.cancelled() and previous.exception() is None):
                raise RuntimeError("this block is already being watched; read its update first")
        task = asyncio.create_task(bot.wait_for_block_update(position, layer=layer, timeout=timeout))
        self._pending[key] = task
        # The SDK subscribes before the server may publish an UpdateBlock.
        await asyncio.sleep(0)
        return {"watching": list(position), "layer": layer}

    async def read_block(self, x: int, y: int, z: int, layer: int = 0) -> dict:
        """Read a new server update from a previously armed watcher."""
        self._joined()
        key = ((x, y, z), layer)
        task = self._pending.pop(key, None)
        if task is None:
            raise RuntimeError("watch_block must be called before read_block")
        update = await task
        self._observed[key] = update
        return {"position": list(update.position), "runtime_id": update.runtime_id, "layer": update.layer}

    async def place_against(self, x: int, y: int, z: int, face: int = 1, slot: int = 0) -> dict:
        """Submit an item-use frame against a server-observed support; not placement acceptance."""
        bot = self._joined()
        against = self._observed.get(((x, y, z), 0))
        if against is None:
            raise RuntimeError("read a server update for the support block first")
        tick = await bot.place_against(against, face=face, slot=slot)
        return {"submitted_tick": tick, "against": list(against.position)}

    async def disconnect(self) -> dict:
        bot, self.bot = self.bot, None
        for task in self._pending.values():
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()
        self._observed.clear()
        if bot is not None:
            await bot.close()
        return {"disconnected": bot is not None}


def create_server():
    from mcp.server.mcpserver import MCPServer

    player = MCPPlayer()
    server = MCPServer("EndPilot")
    server.tool()(player.connect)
    server.tool()(player.watch_block)
    server.tool()(player.read_block)
    server.tool()(player.place_against)
    server.tool()(player.disconnect)
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
