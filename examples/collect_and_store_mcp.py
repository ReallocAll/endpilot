"""Serve the SDK's named collect-and-store task over MCP stdio.

Install the SDK with its MCP extra, then run from the repository root with
``python -m examples.collect_and_store_mcp``. Connect once using the built-in
``connect`` tool. The task tools borrow that player for each run.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

from anyio import CancelScope
from mcp.server.mcpserver.exceptions import ToolError

from endpilot import Bot
from endpilot.collect_and_store import CollectAndStoreResult, collect_and_store
from endpilot.mcp_server import MCPPlayer, create_server
from endpilot.range_transfer import RangeTransferResult

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


class _ObservedBlocks(TypedDict):
    positions: list[tuple[int, int, int]]
    complete: bool


class _Start(TypedDict):
    started: bool


class _Report(TypedDict):
    state: Literal["running", "stopping", "finished", "cancelled", "failed"]
    phase: Literal["collecting", "approaching", "opening", "storing", "closing", "finished"] | None
    picked_up_count: int
    stored_count: int
    transfer: RangeTransferResult | None
    stop_reason: str | None
    error: str | None


@dataclass(slots=True)
class _CollectionRun:
    task: asyncio.Task[None] = field(init=False)
    progress: CollectAndStoreResult | None = None
    error: str | None = None

    def update(self, progress: CollectAndStoreResult) -> None:
        self.progress = progress

    async def stop(self) -> None:
        if not self.task.done():
            if not self.task.cancelling():
                self.task.cancel()
            # An interrupted tool request must not cancel SDK cleanup again.
            await asyncio.wait((self.task,))

    def report(self) -> _Report:
        state = "running"
        progress = self.progress
        reason = progress.stop_reason if progress is not None else None
        if not self.task.done():
            if self.task.cancelling():
                state = "stopping"
        elif self.task.cancelled():
            state = "cancelled"
            reason = reason or "cancelled"
        elif self.error is not None:
            state = "failed"
            reason = reason or "error"
        else:
            state = "finished"

        return {"state": state, "phase": None if progress is None else progress.phase,
                "picked_up_count": 0 if progress is None else progress.picked_up_count,
                "stored_count": 0 if progress is None else progress.stored_count,
                "transfer": None if progress is None else progress.transfer,
                "stop_reason": reason, "error": self.error}


class _CollectAndStoreTools:
    def __init__(self, player: MCPPlayer) -> None:
        self._player = player
        self._run: _CollectionRun | None = None
        player.add_connection_cleanup(self._drain)

    def _connected_bot(self) -> Bot:
        try:
            return self._player.connected_bot()
        except RuntimeError as error:
            raise ToolError(str(error)) from error

    async def find_observed_blocks(self, name: str) -> _ObservedBlocks:
        """Find up to 16 cached blocks by name within 8 blocks without loading terrain."""
        result = self._connected_bot().find_blocks(name, radius=8, limit=16)
        return {"positions": [match.position for match in result.matches], "complete": result.complete}

    async def start_collect_and_store(
        self, positions: list[tuple[int, int, int]], item: str | int, chest: tuple[int, int, int],
        chest_slots: tuple[int, int] | None = None, inventory_slots: tuple[int, int] = (0, 36),
        ticks: int = 160, search_radius: int = 8, timeout: float = 15,
    ) -> _Start:
        """Start collecting ordered blocks and storing matching pickups in a chest.

        The SDK approaches the chest and selects the interaction face. Slot
        pairs are half-open bounds. Omit chest_slots to use all observed slots.
        """
        bot = self._connected_bot()
        if self._run is not None and not self._run.task.done():
            raise ToolError("collect-and-store task is already in progress")
        run = _CollectionRun()

        async def collect() -> None:
            try:
                run.update(await collect_and_store(
                    bot, positions, item=item, chest=chest,
                    chest_slots=None if chest_slots is None else range(*chest_slots),
                    inventory_slots=range(*inventory_slots), ticks=ticks,
                    search_radius=search_radius, timeout=timeout, on_progress=run.update,
                ))
            except asyncio.CancelledError as error:
                run.progress = getattr(error, "collect_and_store_progress", run.progress)
                raise
            except Exception as error:
                run.progress = getattr(error, "collect_and_store_progress", run.progress)
                run.error = str(error) or type(error).__name__

        run.task = asyncio.create_task(collect())
        self._run = run
        return {"started": True}

    def _current_run(self) -> _CollectionRun:
        self._connected_bot()
        if self._run is None:
            raise ToolError("start_collect_and_store must be called first")
        return self._run

    async def observe_collect_and_store(self) -> _Report:
        """Read the latest phase and confirmed counts, including after completion.

        A null confirmed count means the evidence was insufficient to establish
        the quantity moved. The verdict can be SUCCESS when the source and
        destination counts disagree.
        """
        return self._current_run().report()

    async def stop_collect_and_store(self) -> _Report:
        """Cancel the current task and wait for its settled evidence and cleanup."""
        run = self._current_run()
        await run.stop()
        return run.report()

    async def _drain(self) -> None:
        run = self._run
        if run is None:
            return
        interrupted = None
        with CancelScope(shield=True):
            while not run.task.done():
                try:
                    await run.stop()
                except asyncio.CancelledError as error:
                    # A direct Task.cancel() can still interrupt a shielded scope.
                    interrupted = error
        self._run = None
        if interrupted is not None:
            raise interrupted


def register_tools(server: "MCPServer", player: MCPPlayer) -> None:
    """Add the named task to a server using its existing connected player."""
    tools = _CollectAndStoreTools(player)
    server.tool()(tools.find_observed_blocks)
    server.tool()(tools.start_collect_and_store)
    server.tool()(tools.observe_collect_and_store)
    server.tool()(tools.stop_collect_and_store)


if __name__ == "__main__":
    create_server(register_tools).run(transport="stdio")
