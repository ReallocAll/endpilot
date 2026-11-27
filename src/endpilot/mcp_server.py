"""Stdio MCP tools over the same player actions used by ordinary Python scripts."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from enum import Enum
from functools import wraps
import logging
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from urllib.parse import urlsplit

from ._join_address import _parse_join_address
from ._mcp_accounts import AccountError, MCPAccounts
from .blocks import BlockChange, BlockUpdate
from .bot import Bot
from .inventory import HeldItemStack
from .login import LoginRefused, SessionEnd
from .navigation import BlockInteractionGoal, BlockInteractionMatch, NavigationResult, NavigationRules
from .resource_packs import ResourcePackDecision, ResourcePackError

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


_LOGGER = logging.getLogger(__name__)
_PACK_HASH_MISMATCH = "resource-pack SHA-256 does not match the received archive"


class _JoinError(RuntimeError):
    """A join failure with a message safe for the caller."""


@dataclass(frozen=True, slots=True)
class _ConnectionTarget:
    url: str
    name: str
    xuid: str | None = None
    accept_resource_packs: bool = False

    def __post_init__(self) -> None:
        if self.xuid is not None:
            try:
                from .xbox._cache import _valid_xuid
            except ImportError:
                raise AccountError("Install endpilot[xbox] to join with a remembered Xbox account.") from None

            if not _valid_xuid(self.xuid):
                raise ValueError("use an Xbox XUID from account_list")


class _JoinFailure(TypedDict):
    error_type: str
    reason_name: str | None
    message: str | None


class _ShutdownReply(TypedDict):
    shutdown: Literal[True]


class _SessionStatus(TypedDict):
    state: Literal["idle", "joining", "connected", "disconnecting", "disconnected", "join_failed"]
    server: str | None
    name: str | None
    xuid: str | None
    player_id: int | None
    last_end: SessionEnd | None
    last_join_failure: _JoinFailure | None


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
        self._target: _ConnectionTarget | None = None
        self._joining = False
        self._join_generation = 0
        self._session_watch: asyncio.Task[None] | None = None
        self._last_end: SessionEnd | None = None
        self._join_failure: _JoinFailure | None = None
        self._connection_cleanups: list[Callable[[], Awaitable[None]]] = []
        self._accounts = MCPAccounts()

    def connected_bot(self) -> Bot:
        """Borrow the current Bot for a tool call without taking ownership."""
        if self.bot is None or self._transferring or self.bot.session_end is not None:
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
                account_close = asyncio.create_task(self._accounts.close())
                try:
                    await self.disconnect()
                finally:
                    await account_close

    async def session_status(self) -> _SessionStatus:
        """Observe the current connection and the latest end or join failure."""
        bot, target = self.bot, self._target
        end = None if bot is None else bot.session_end
        if self._joining:
            state = "joining"
        elif self._transferring or end is not None:
            state = "disconnecting"
        elif bot is not None:
            state = "connected"
        elif self._join_failure is not None:
            state = "join_failed"
        elif target is not None:
            state = "disconnected"
        else:
            state = "idle"
        server = None
        if target is not None:
            url = urlsplit(target.url)
            server = f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}"
        name = None if target is None else target.name
        if state == "connected" and target is not None and target.xuid is not None:
            name = bot.identity.name
        return {"state": state, "server": server, "name": name,
                "xuid": None if target is None else target.xuid,
                "player_id": bot.player_id if state == "connected" else None,
                "last_end": end or self._last_end, "last_join_failure": self._join_failure}

    async def connect(self, url: str, name: str = "EndPilot", xuid: str | None = None, *,
                      accept_resource_packs: bool = False) -> dict:
        """Join a server explicitly, optionally accepting its resource packs."""
        if type(accept_resource_packs) is not bool:
            raise TypeError("accept_resource_packs must be a boolean")
        async with self._lifecycle:
            if self.bot is not None and self.bot.session_end is None:
                raise RuntimeError("a player is already connected")
            if self.bot is not None:
                await self._disconnect_owned(self.bot)
            return await self._join(_ConnectionTarget(url, name, xuid, accept_resource_packs=accept_resource_packs))

    async def reconnect(self) -> dict:
        """Explicitly open a fresh connection using the last requested server and identity."""
        async with self._admit_target() as target:
            return await self._replace_connection(target)

    async def switch_account(self, xuid: str | None = None, name: str | None = None) -> dict:
        """Join the last server as a remembered Xbox account or as an offline name."""
        if xuid is None and name is None:
            raise ValueError("select a remembered Xbox XUID or provide an offline name")
        async with self._admit_target() as target:
            return await self._replace_connection(replace(target, xuid=xuid, name=target.name if name is None else name))

    @asynccontextmanager
    async def _admit_target(self) -> AsyncIterator[_ConnectionTarget]:
        target, generation = self._target, self._join_generation
        if self._joining:
            raise _JoinError("player connection is changing; retry after the join completes")
        if target is None:
            raise _JoinError("connect a player first")
        async with self._lifecycle:
            if self._target is not target or self._join_generation != generation:
                raise _JoinError("player connection changed while this request was waiting; retry explicitly")
            yield target

    async def _replace_connection(self, target: _ConnectionTarget) -> dict:
        if self.bot is not None:
            await self._disconnect_owned(self.bot)
        return await self._join(target)

    async def _join(self, target: _ConnectionTarget) -> dict:
        self._join_generation += 1
        self._joining = True
        self._join_failure = None
        bot = None
        try:
            url = target.url
            # urlsplit strips tabs and line breaks before exposing the hostname.
            try:
                if any(ord(char) < 32 or ord(char) == 127 for char in url):
                    raise ValueError
                address = urlsplit(url)
                if address.scheme == "raknet":
                    raknet_address = _parse_join_address(url)
                elif address.scheme in ("http", "https"):
                    raknet_address = None
                    host = address.hostname
                    if not host or address.port == 0 or any(char.isspace() for char in host):
                        raise ValueError
                else:
                    raise ValueError
            except ValueError:
                self._join_failure = {"error_type": "ValueError", "reason_name": "INVALID_ADDRESS", "message": None}
                raise ValueError("use a valid NetherNet signaling URL or raknet://host:port") from None

            self._target = target
            try:
                bot = Bot(target.name)
                options = {}
                if target.xuid is not None:
                    options["credential_provider"] = self._accounts.credential_provider(bot, xuid=target.xuid)
                if target.accept_resource_packs:
                    options["on_resource_packs"] = lambda _offer: ResourcePackDecision.ACCEPT
                if raknet_address is not None:
                    await bot.join_raknet(raknet_address.host, raknet_address.port, **options)
                else:
                    await bot.join_nethernet(url, **options)
            except asyncio.CancelledError:
                if bot is not None:
                    await self._finish_cleanup(bot.close())
                raise
            except Exception as error:
                reason = getattr(error, "error", None)
                reason_name = reason.name if isinstance(reason, Enum) else None
                message = None
                if isinstance(error, LoginRefused):
                    reason_name = error.reason_name
                    message = error.server_message
                elif isinstance(error, AccountError):
                    message = str(error)
                elif isinstance(error, ResourcePackError) and str(error) == _PACK_HASH_MISMATCH:
                    message = _PACK_HASH_MISMATCH
                self._join_failure = {"error_type": type(error).__name__, "reason_name": reason_name,
                                      "message": message}
                if bot is not None:
                    try:
                        await self._finish_cleanup(bot.close())
                    except Exception as cleanup_error:
                        _LOGGER.error("Failed to close unjoined player (%s)", type(cleanup_error).__name__)
                detail = ": ".join(part for part in (type(error).__name__, reason_name, message) if part)
                raise _JoinError(f"Join failed ({detail})") from None
            self.bot = bot
            self._actions = set()
            self._session_watch = asyncio.create_task(self._watch_session_end(bot))
            return {"name": target.name if target.xuid is None else bot.identity.name, "player_id": bot.player_id}
        finally:
            self._joining = False

    async def _watch_session_end(self, bot: Bot) -> None:
        try:
            await bot.wait_for_session_end(timeout=None)
            async with self._lifecycle:
                if self.bot is bot:
                    await self._disconnect_owned(bot)
        except Exception as error:
            _LOGGER.error("Player session cleanup failed (%s)", type(error).__name__)

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
        try:
            await bot.close()
        finally:
            self._last_end = bot.session_end

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

    @staticmethod
    async def _finish_cleanup(operation: Coroutine[Any, Any, None]) -> None:
        cleanup = asyncio.create_task(operation)
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

    def _stop_session_watch(self) -> None:
        watcher, self._session_watch = self._session_watch, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()

    async def _disconnect_owned(self, bot: Bot) -> None:
        self._transferring = True
        self._stop_session_watch()
        try:
            await self._finish_cleanup(self._drain_connection(bot, disconnect=True))
        finally:
            self._transferring = False

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
            if self.bot is not bot or bot.session_end is not None:
                raise RuntimeError("player connection changed before handoff")
            self._transferring = True
            try:
                await self._drain_connection(bot, disconnect=False)
                if self._actions:
                    await asyncio.wait(tuple(self._actions))
                if bot.session_end is not None:
                    raise RuntimeError("player connection ended before handoff")
                self._stop_session_watch()
                self.bot = None
                self._target = None
                return bot
            finally:
                self._transferring = False

    async def disconnect(self) -> dict:
        """Leave the current player session and keep the stdio server available."""
        bot = self.bot
        async with self._lifecycle:
            if bot is None or self.bot is not bot:
                return {"disconnected": False}
            await self._disconnect_owned(bot)
            return {"disconnected": True}


def create_server(*extensions: Callable[["MCPServer", MCPPlayer], None]) -> "MCPServer":
    """Register extra tools against the same player as the built-in tools."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from pydantic import StrictBool

    from ._mcp_scripts import MCPScripts

    player = MCPPlayer()
    scripts = MCPScripts(player)
    player.add_connection_cleanup(scripts.close)
    server = MCPServer("EndPilot", lifespan=player.lifespan)

    async def connect(url: str, name: str = "EndPilot", xuid: str | None = None, *,
                      accept_resource_packs: StrictBool = False) -> dict:
        """Join a server explicitly. Use xuid for a remembered Xbox account.

        Set accept_resource_packs to true to download and verify the server's resource packs.
        """
        return await player.connect(url, name, xuid, accept_resource_packs=accept_resource_packs)

    def register_join(tool):
        @wraps(tool)
        async def report_failure(*args, **kwargs):
            try:
                return await tool(*args, **kwargs)
            except (_JoinError, AccountError) as error:
                raise ToolError(str(error)) from None

        server.tool()(report_failure)

    register_join(connect)
    server.tool()(player.session_status)
    register_join(player.reconnect)
    register_join(player.switch_account)
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
    player._accounts.register(server)
    scripts.register(server)
    for register_tools in extensions:
        register_tools(server, player)
    return server


async def _run_stdio() -> None:
    import sys

    from anyio import wrap_file
    from mcp.server import stdio
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse, jsonrpc_message_adapter
    from pydantic import ValidationError

    class Stdio:
        def __init__(self, stdin, stdout):
            self.stdin = stdin
            self.stdout = stdout
            self.pending_id: int | str | None = None
            self.flushing: bool | None = None
            self.flushed = asyncio.Event()
            self.stopping = False

        async def __aiter__(self) -> AsyncIterator[str]:
            async for line in self.stdin:
                try:
                    request = jsonrpc_message_adapter.validate_json(line, by_name=False)
                except ValidationError:
                    request = None  # Leave parse errors to the SDK.
                if (isinstance(request, JSONRPCRequest) and request.method == "tools/call"
                        and (request.params or {}).get("name") == "shutdown"):
                    self.pending_id = request.id
                yield line
                if self.pending_id is not None:
                    # A pending physical readline cannot be interrupted by an MCP reply.
                    await self.flushed.wait()
                    if self.stopping:
                        return  # Let the SDK handle normal input EOF and run its lifespan cleanup.
                    self.pending_id = None
                    self.flushed.clear()

        async def write(self, line: str) -> int:
            if self.pending_id is not None:
                reply = jsonrpc_message_adapter.validate_json(line, by_name=False)
                if isinstance(reply, (JSONRPCResponse, JSONRPCError)) and reply.id == self.pending_id:
                    result = reply.result if isinstance(reply, JSONRPCResponse) else {}
                    structured = result.get("structuredContent")
                    self.flushing = (not result.get("isError") and isinstance(structured, dict)
                                     and structured.get("shutdown") is True)
            return await self.stdout.write(line)

        async def flush(self) -> None:
            await self.stdout.flush()
            if self.flushing is not None:
                self.stopping = self.flushing
                self.flushing = None
                self.flushed.set()

    server = create_server()

    @server.tool()
    async def shutdown(ctx: Context) -> _ShutdownReply:
        """Close this stdio server and release the player, scripts and pending authorization."""
        if (ctx.request_context.params or {}).get("arguments"):
            raise ToolError("shutdown does not accept arguments")
        return {"shutdown": True}

    # The SDK claims both descriptors so unrelated reads and prints cannot reach the wire.
    with ExitStack() as claims:
        raw_input, restore_input = stdio._claim_fd(0, sys.stdin, "rb", stdio._open_stdin_diversion)
        if restore_input is not None:
            claims.callback(restore_input)
        raw_output, restore_output = stdio._claim_fd(1, sys.stdout, "wb", stdio._open_stdout_diversion)
        if restore_output is not None:
            claims.callback(restore_output)
        streams = Stdio(
            wrap_file(stdio._UnownedTextWrapper(raw_input, encoding="utf-8", errors="replace")),
            wrap_file(stdio._UnownedTextWrapper(raw_output, encoding="utf-8")),
        )
        kernel = server._lowlevel_server
        async with stdio.stdio_server(stdin=streams, stdout=streams) as (incoming, outgoing):
            await kernel.run(incoming, outgoing, kernel.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_run_stdio())
