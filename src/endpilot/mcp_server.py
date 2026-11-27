"""Stdio MCP tools over the same player actions used by ordinary Python scripts."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlsplit

from ._join_address import _parse_join_address
from .blocks import BlockChange, BlockUpdate
from .bot import Bot
from .inventory import HeldItemStack
from .navigation import BlockInteractionGoal, BlockInteractionMatch, NavigationResult, NavigationRules

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


class _BlockSnapshot(TypedDict):
    position: tuple[int, int, int]
    layer: int
    observed: bool
    runtime_id: int | None
    is_air: bool | None


class _StackSnapshot(TypedDict):
    item_id: int
    count: int
    aux_value: int
    net_id: int | None
    block_runtime_id: int
    user_data_hex: str


class _InventoryChange(TypedDict):
    before: _StackSnapshot
    after: _StackSnapshot


class _PlacementReport(TypedDict):
    dimension_id: int
    target: tuple[int, int, int]
    slot: int
    submitted: bool | None
    reason: str
    world_change: BlockChange | None
    inventory_change: _InventoryChange | None


class _PlanCost(TypedDict):
    numerator: int
    denominator: int


class _InteractionPlan(TypedDict):
    target: tuple[int, int, int]
    dimension_id: int
    start: tuple[int, int, int] | None
    status: str
    nodes: tuple[tuple[int, int, int], ...]
    endpoint: tuple[int, int, int] | None
    matches: tuple[BlockInteractionMatch, ...]
    cost: _PlanCost | None
    expanded: int


class _InteractionStart(TypedDict):
    started: bool
    action: str
    target: tuple[int, int, int]
    requested_frames: int


class _SectionRequest(TypedDict):
    requested: bool
    position: tuple[int, int, int]


@dataclass(slots=True)
class _MovementAction:
    owner: Bot
    task: asyncio.Task[int | NavigationResult]
    goal: BlockInteractionGoal | None = None


def _stack_snapshot(stack: HeldItemStack) -> _StackSnapshot:
    return {"item_id": stack.item_id, "count": stack.count, "aux_value": stack.aux_value,
            "net_id": stack.net_id, "block_runtime_id": stack.block_runtime_id,
            "user_data_hex": stack.user_data_buffer.hex()}


class MCPPlayer:
    def __init__(self):
        self.bot: Bot | None = None
        self._pending: dict[tuple[tuple[int, int, int], int], asyncio.Task[BlockUpdate]] = {}
        self._observed: dict[tuple[tuple[int, int, int], int], BlockUpdate] = {}
        self._reading: set[asyncio.Task] = set()
        self._movement: _MovementAction | None = None
        self._actions: set[asyncio.Future[None]] = set()
        self._lifecycle = asyncio.Lock()
        self._transferring = False
        self._connection_cleanups: list[Callable[[], Awaitable[None]]] = []

    def connected_bot(self) -> Bot:
        """Borrow the current Bot for a tool call without taking ownership."""
        if self.bot is None or self._transferring:
            raise RuntimeError("connect a player first")
        return self.bot

    @contextmanager
    def _connection_action(self):
        bot = self.connected_bot()
        pending = self._actions
        settled = asyncio.get_running_loop().create_future()
        pending.add(settled)
        try:
            yield bot
        finally:
            pending.remove(settled)
            settled.set_result(None)

    def add_connection_cleanup(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        """Register cleanup to run before the current Bot is closed or handed off."""
        self._connection_cleanups.append(cleanup)

    @asynccontextmanager
    async def lifespan(self, _server: "MCPServer") -> AsyncIterator[None]:
        from anyio import CancelScope

        try:
            yield
        finally:
            with CancelScope(shield=True):
                await self.disconnect()

    async def connect(self, url: str, name: str = "EndPilot") -> dict:
        async with self._lifecycle:
            if self.bot is not None:
                raise RuntimeError("a player is already connected")
            # urlsplit strips tabs and line breaks before exposing the hostname.
            if any(ord(char) < 32 or ord(char) == 127 for char in url):
                raise ValueError("invalid server address")
            target = urlsplit(url)
            if target.scheme == "raknet":
                raknet_address = _parse_join_address(url)
            elif target.scheme in ("http", "https"):
                raknet_address = None
                host = target.hostname
                if not host or target.port == 0 or any(char.isspace() for char in host):
                    raise ValueError("invalid server address")
            else:
                raise ValueError("use a NetherNet signaling URL or raknet://host:port")
            bot = Bot(name)
            try:
                if raknet_address is not None:
                    await bot.join_raknet(raknet_address.host, raknet_address.port)
                else:
                    await bot.join_nethernet(url)
            except BaseException:
                await bot.close()
                raise
            self.bot = bot
            self._actions = set()
            return {"name": name, "player_id": bot.player_id}

    async def watch_block(self, x: int, y: int, z: int, layer: int = 0, timeout: float = 15) -> dict:
        """Arm a server UpdateBlock watcher before causing a world change."""
        bot = self.connected_bot()
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
        self.connected_bot()
        key = ((x, y, z), layer)
        task = self._pending.pop(key, None)
        if task is None:
            raise RuntimeError("watch_block must be called before read_block")
        reader = asyncio.current_task()
        self._reading.add(reader)
        try:
            update = await task
            self._observed[key] = update
            return {"position": list(update.position), "runtime_id": update.runtime_id, "layer": update.layer}
        finally:
            self._reading.discard(reader)

    async def place_against(self, x: int, y: int, z: int, face: int = 1, slot: int = 0) -> dict:
        """Submit an item-use frame against a server-observed support; not placement acceptance."""
        with self._connection_action() as bot:
            against = self._observed.get(((x, y, z), 0))
            if against is None:
                raise RuntimeError("read a server update for the support block first")
            tick = await bot.place_against(against, face=face, slot=slot)
        return {"submitted_tick": tick, "against": list(against.position)}

    async def observed_block(self, x: int, y: int, z: int, layer: int = 0) -> _BlockSnapshot:
        """Read the latest block snapshot in the current dimension without sending a packet."""
        position = (x, y, z)
        block = self.connected_bot().observed_block(position, layer=layer)
        return {"position": position, "layer": layer, "observed": block is not None,
                "runtime_id": None if block is None else block.runtime_id,
                "is_air": None if block is None else block.is_air}

    async def place_block(
        self, x: int, y: int, z: int, expected_runtime_id: int, face: int = 1,
        click_pos: tuple[float, float, float] | None = None, timeout: float = 15,
    ) -> _PlacementReport:
        """Use the selected survival item and report independent world and slot observations."""
        with self._connection_action() as bot:
            try:
                result = await bot.place_block((x, y, z), face=face, expected_runtime_id=expected_runtime_id,
                                               click_pos=click_pos, timeout=timeout)
            except asyncio.CancelledError as error:
                result = getattr(error, "placement_progress", None)
                if result is None:
                    raise
        change = result.inventory_change
        return {"dimension_id": result.dimension_id, "target": result.target, "slot": result.slot,
                "submitted": result.submitted, "reason": result.reason, "world_change": result.world_change,
                "inventory_change": None if change is None else {
                    "before": _stack_snapshot(change.before), "after": _stack_snapshot(change.after),
                }}

    async def start_walk(self, ticks: int = 160) -> dict:
        """Start a bounded walk without blocking the MCP client."""
        bot = self.connected_bot()
        self._check_movement_slot()
        self._movement = _MovementAction(bot, asyncio.create_task(bot.walk_forward(ticks)))
        await asyncio.sleep(0)
        return {"started": True, "requested_frames": ticks}

    async def plan_block_interaction(
        self, x: int, y: int, z: int, max_distance: float = 5.0, search_radius: int = 8,
        vertical_radius: int = 4, max_expansions: int = 16384,
    ) -> _InteractionPlan:
        """Plan a standing position with a clear hit on the target using observed terrain."""
        goal = BlockInteractionGoal((x, y, z), max_distance)
        rules = NavigationRules(search_radius, vertical_radius, max_expansions)
        plan = self.connected_bot().plan_navigation(goal, rules=rules)
        cost = None if plan.cost is None else {"numerator": plan.cost.numerator,
                                               "denominator": plan.cost.denominator}
        return {"target": goal.position, "dimension_id": plan.dimension_id, "start": plan.start,
                "status": plan.status, "nodes": plan.nodes, "endpoint": plan.endpoint,
                "matches": plan.matches, "cost": cost, "expanded": plan.expanded}

    async def start_block_interaction(
        self, x: int, y: int, z: int, max_distance: float = 5.0, search_radius: int = 8,
        vertical_radius: int = 4, max_expansions: int = 16384, ticks: int = 160, timeout: float = 15,
    ) -> _InteractionStart:
        """Start bounded navigation to a block hit. Read its outcome with observe_walk."""
        bot = self.connected_bot()
        self._check_movement_slot()
        goal = BlockInteractionGoal((x, y, z), max_distance)
        rules = NavigationRules(search_radius, vertical_radius, max_expansions)
        task = asyncio.create_task(bot.navigate(goal, rules=rules, ticks=ticks, timeout=timeout))
        self._movement = _MovementAction(bot, task, goal)
        await asyncio.sleep(0)
        return {"started": True, "action": "block_interaction", "target": goal.position, "requested_frames": ticks}

    async def request_sub_chunk(self, x: int, y: int, z: int) -> _SectionRequest:
        """Request the section containing these world block coordinates."""
        position = (x, y, z)
        with self._connection_action() as bot:
            await bot.request_sub_chunk(position)
        return {"requested": True, "position": position}

    def _check_movement_slot(self) -> None:
        if self._movement is not None or self._lifecycle.locked():
            raise RuntimeError("observe or stop the previous walk first")

    def _current_movement(self) -> _MovementAction:
        self.connected_bot()
        if self._movement is None:
            raise RuntimeError("start_walk must be called first")
        return self._movement

    @staticmethod
    def _movement_outcome(action: _MovementAction, stopped: bool | None = None) -> dict:
        position = action.owner.position
        if action.goal is None:
            if action.task.cancelled() and stopped is not None:
                return {"state": "cancelled", "submitted_frames": None, "server_position": position}
            return {"state": "stopped" if stopped else "finished", "submitted_frames": action.task.result(),
                    "server_position": position}
        result = None if action.task.cancelled() else action.task.result()
        state = "cancelled" if result is None else "stopped" if result.status == "stopped" else "finished"
        return {"state": state, "action": "block_interaction", "target": action.goal.position,
                "status": None if result is None else result.status,
                "submitted_frames": None if result is None else result.frames,
                "expanded": None if result is None else result.expanded,
                "stand_position": None if result is None else result.stand_position,
                "matches": [] if result is None else [asdict(match) for match in result.matches],
                "server_position": position}

    async def observe_walk(self) -> dict:
        """Report progress and the last server-reported position."""
        action = self._current_movement()
        stopping = self._lifecycle.locked()
        if not action.task.done() or stopping:
            result = {"state": "running", "server_position": action.owner.position}
            if action.goal is not None:
                result.update(state="stopping" if stopping else "running", action="block_interaction",
                              target=action.goal.position)
            return result
        self._movement = None
        return self._movement_outcome(action)

    async def stop_walk(self) -> dict:
        """Stop a running walk and wait for release of the movement input."""
        action = self._current_movement()
        if self._lifecycle.locked():
            raise RuntimeError("movement is already stopping")
        async with self._lifecycle:
            return await self._stop_movement(action)

    async def _stop_movement(self, action: _MovementAction) -> dict:
        if self._movement is not action or self.bot is not action.owner:
            raise RuntimeError("movement changed before it could be stopped")
        task = action.task
        was_running = not task.done()
        if was_running:
            await action.owner.stop_moving()
            if not task.done():
                task.cancel()
                await asyncio.wait((task,))
        self._movement = None
        return self._movement_outcome(action, stopped=was_running)

    async def _cancel_block_watchers(self) -> None:
        tasks = [*self._pending.values(), *self._reading]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._reading.clear()
        self._observed.clear()

    async def _cancel_movement(self, action: _MovementAction) -> None:
        action.task.cancel()
        await asyncio.gather(action.task, return_exceptions=True)
        self._movement = None

    async def _close_bot(self, bot: Bot) -> None:
        self.bot = None
        await bot.close()

    async def _drain_connection(self, bot: Bot, *, disconnect: bool) -> None:
        async with AsyncExitStack() as stack:
            if disconnect:
                stack.push_async_callback(self._close_bot, bot)
            stack.push_async_callback(self._cancel_block_watchers)
            if self._movement is not None:
                stop = self._cancel_movement if disconnect else self._stop_movement
                stack.push_async_callback(stop, self._movement)
            # The stack unwinds in reverse, so extension cleanup runs in registration order.
            for cleanup in reversed(self._connection_cleanups):
                stack.push_async_callback(cleanup)

    async def _finish_disconnect(self, bot: Bot) -> None:
        cleanup = asyncio.create_task(self._drain_connection(bot, disconnect=True))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as cancellation:
            # MCP cancels at every checkpoint until the request leaves its scope.
            # Python callers can use MCPPlayer without the optional MCP dependencies.
            try:
                from anyio import CancelScope
            except ImportError:
                scope = nullcontext()
            else:
                scope = CancelScope(shield=True)
            with scope:
                while not cleanup.done():
                    try:
                        await asyncio.wait((cleanup,))
                    except asyncio.CancelledError:
                        continue
            if not cleanup.cancelled() and (failure := cleanup.exception()) is not None:
                raise cancellation from failure
            raise

    async def handoff(self) -> Bot:
        """Return the live Bot to Python code in this process, without reconnecting.

        This is a Python ownership transfer, not an MCP stdio tool: Bot objects
        cannot be serialized into a different process. Admitted built-in writes
        finish before transfer. Cancelling the wait leaves the Bot with MCP.
        """
        bot = self.bot
        if bot is None:
            raise RuntimeError("connect a player first")
        async with self._lifecycle:
            if self.bot is not bot:
                raise RuntimeError("player connection changed before handoff")
            self._transferring = True
            try:
                await self._drain_connection(bot, disconnect=False)
                if self._actions:
                    await asyncio.wait(tuple(self._actions))
                self.bot = None
                return bot
            finally:
                self._transferring = False

    async def disconnect(self) -> dict:
        bot = self.bot
        async with self._lifecycle:
            if bot is None or self.bot is not bot:
                return {"disconnected": False}
            self._transferring = True
            try:
                await self._finish_disconnect(bot)
                return {"disconnected": True}
            finally:
                self._transferring = False


def create_server(*extensions: Callable[["MCPServer", MCPPlayer], None]) -> "MCPServer":
    """Register extra tools against the same player as the built-in tools."""
    from mcp.server.mcpserver import MCPServer

    player = MCPPlayer()
    server = MCPServer("EndPilot", lifespan=player.lifespan)
    server.tool()(player.connect)
    server.tool()(player.watch_block)
    server.tool()(player.read_block)
    server.tool()(player.place_against)
    server.tool()(player.start_walk)
    server.tool()(player.observe_walk)
    server.tool()(player.stop_walk)
    server.tool()(player.disconnect)
    server.tool()(player.observed_block)
    server.tool()(player.place_block)
    server.tool()(player.plan_block_interaction)
    server.tool()(player.start_block_interaction)
    server.tool()(player.request_sub_chunk)
    for register_tools in extensions:
        register_tools(server, player)
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
