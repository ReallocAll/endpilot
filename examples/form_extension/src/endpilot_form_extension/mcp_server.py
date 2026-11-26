"""Run as a standalone MCP server or pass register_tools to EndPilot's create_server."""

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING

from endpilot import Bot
from endpilot.mcp_server import MCPPlayer
from endpilot.protocol import RawPacket

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

class _FormTools:
    def __init__(self, player: MCPPlayer):
        self._player = player
        self._waiting: asyncio.Task[RawPacket] | None = None
        self._form: RawPacket | None = None
        self._sending: set[asyncio.Task[None]] = set()
        player.add_connection_cleanup(self._clear_forms)

    def _check_owner(self, bot: Bot) -> None:
        if self._player.connected_bot() is not bot:
            raise RuntimeError("player connection changed")

    async def _wait_owned[T](self, bot: Bot, task: asyncio.Task[T]) -> T:
        try:
            result = await task
        except asyncio.CancelledError:
            # Teardown cancels the work, while the MCP request can still report an error.
            if not asyncio.current_task().cancelling():
                self._check_owner(bot)
            raise
        self._check_owner(bot)
        return result

    async def _send(self, bot: Bot, action: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(action)
        self._sending.add(task)
        try:
            await self._wait_owned(bot, task)
        finally:
            self._sending.discard(task)

    async def watch_form(self, timeout: float = 15) -> dict:
        """Subscribe before the action that asks the server to send a form."""
        bot = self._player.connected_bot()
        if self._waiting is not None or (
            self._form is not None and bot.get_pending_form(self._form) is not None
        ):
            raise RuntimeError("read or answer the preceding form first")
        self._form = None
        packet_id = bot.protocol.binding.ModalFormRequestPacket.ID
        self._waiting = asyncio.create_task(
            bot.packets.wait_for_packet(packet_id, timeout=timeout)
        )
        await asyncio.sleep(0)
        self._check_owner(bot)
        return {"watching": True}

    async def send_chat(self, message: str) -> dict:
        """Submit chat, not proof of server delivery."""
        bot = self._player.connected_bot()
        await self._send(bot, bot.send_chat(message))
        return {"submitted": True}

    async def read_form(self) -> dict:
        """Return the server-sent action form, without choosing a button."""
        bot = self._player.connected_bot()
        if self._waiting is None:
            raise RuntimeError("watch_form must be called first")
        task = self._waiting
        try:
            request = await self._wait_owned(bot, task)
        finally:
            if self._waiting is task:
                self._waiting = None
        pending = bot.get_pending_form(request)
        if pending is None:
            raise RuntimeError("form is no longer pending")
        form = pending["form"]
        if not isinstance(form, dict) or form.get("type") != "form":
            raise ValueError("expected a server action form")
        if "elements" in form:
            count = sum(
                1 for element in form["elements"]
                if isinstance(element, dict) and element.get("type") == "button"
            )
        else:
            count = len(form.get("buttons", []))
        self._form = request
        return {"form_id": pending["form_id"], "title": form.get("title"),
                "button_count": count, "form": form}

    async def answer_form(self, form_id: int, button_index: int) -> dict:
        """Submit an answer for the observed form; BDS acceptance is separate."""
        bot = self._player.connected_bot()
        request = self._form
        if request is None:
            raise RuntimeError("read the matching server form before answering")
        pending = bot.get_pending_form(request)
        if pending is None or pending["form_id"] != form_id:
            raise RuntimeError("read the matching server form before answering")
        await self._send(bot, bot.answer_form(button_index, request))
        if self._form is request:
            self._form = None
        return {"form_id": form_id, "title": pending["form"].get("title"),
                "button_index": button_index, "submitted": True}

    async def _clear_forms(self) -> None:
        tasks = list(self._sending)
        if self._waiting is not None:
            tasks.append(self._waiting)
        self._waiting = None
        self._form = None
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _register_tools(self, server: "MCPServer") -> None:
        server.tool()(self.watch_form)
        server.tool()(self.send_chat)
        server.tool()(self.read_form)
        server.tool()(self.answer_form)


class FormPlayer(_FormTools):
    def __init__(self):
        super().__init__(MCPPlayer())

    @property
    def bot(self) -> Bot | None:
        return self._player.bot

    @bot.setter
    def bot(self, bot: Bot | None) -> None:
        self._player.bot = bot

    async def connect(self, url: str, name: str = "Knight") -> dict:
        return await self._player.connect(url, name)

    async def disconnect(self) -> dict:
        return await self._player.disconnect()


def register_tools(server: "MCPServer", player: MCPPlayer) -> None:
    """Add form tools that borrow the built-in server's connected player."""
    _FormTools(player)._register_tools(server)


def create_server() -> "MCPServer":
    from mcp.server.mcpserver import MCPServer

    player = FormPlayer()
    server = MCPServer("EndPilotActionForms", lifespan=player._player.lifespan)
    server.tool()(player.connect)
    player._register_tools(server)
    server.tool()(player.disconnect)
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
