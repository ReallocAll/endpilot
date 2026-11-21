"""MCP action-form extension; only the public EndPilot packet API is used."""

import asyncio
import json

from endpilot import Bot


class FormPlayer:
    def __init__(self):
        self.bot: Bot | None = None
        self._waiting: asyncio.Task | None = None
        self._form: tuple[int, object, int] | None = None

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
                host, sep, port = url.removeprefix("raknet://").rpartition(":")
                if not sep:
                    host, port = port, "19132"
                await bot.join_raknet(host, int(port))
            else:
                await bot.join_nethernet(url)
        except BaseException:
            await bot.close()
            raise
        self.bot = bot
        return {"name": name, "player_id": bot.player_id}

    async def watch_form(self, timeout: float = 15) -> dict:
        """Subscribe before the action that asks the server to send a form."""
        bot = self._joined()
        if self._waiting is not None or self._form is not None:
            raise RuntimeError("read or answer the preceding form first")
        packet_id = bot.protocol.binding.ModalFormRequestPacket.ID
        self._waiting = asyncio.create_task(
            bot.packets.wait_for_packet(packet_id, timeout=timeout)
        )
        await asyncio.sleep(0)
        return {"watching": True}

    async def send_chat(self, message: str) -> dict:
        """Submit chat, not proof of server delivery."""
        await self._joined().send_chat(message)
        return {"submitted": True}

    async def read_form(self) -> dict:
        """Return the server-sent action form, without choosing a button."""
        bot = self._joined()
        if self._waiting is None:
            raise RuntimeError("watch_form must be called first")
        task, self._waiting = self._waiting, None
        packet = bot.protocol.decode(await task)
        form = json.loads(packet.form_json)
        if not isinstance(form, dict) or form.get("type") != "form":
            raise ValueError("expected a server action form")
        if "elements" in form:
            count = sum(
                1 for element in form["elements"]
                if isinstance(element, dict) and element.get("type") == "button"
            )
        else:
            count = len(form.get("buttons", []))
        form_id = int(packet.form_id)
        self._form = (form_id, form.get("title"), count)
        return {"form_id": form_id, "title": form.get("title"),
                "button_count": count, "form": form}

    async def answer_form(self, form_id: int, button_index: int) -> dict:
        """Submit an answer for the observed form; BDS acceptance is separate."""
        bot = self._joined()
        if self._form is None or form_id != self._form[0]:
            raise RuntimeError("read the matching server form before answering")
        observed_id, title, count = self._form
        if (isinstance(button_index, bool) or not isinstance(button_index, int)
                or not 0 <= button_index < count):
            raise ValueError("button index is outside the server's action form")
        await bot.packets.send(bot.protocol.binding.ModalFormResponsePacket(
            form_id=observed_id,
            json_response=json.dumps(button_index),
            form_cancel_reason=None,
        ))
        self._form = None
        return {"form_id": observed_id, "title": title,
                "button_index": button_index, "submitted": True}

    async def disconnect(self) -> dict:
        bot, self.bot = self.bot, None
        if self._waiting is not None:
            self._waiting.cancel()
            await asyncio.gather(self._waiting, return_exceptions=True)
        self._waiting = None
        self._form = None
        if bot is not None:
            await bot.close()
        return {"disconnected": bot is not None}


def create_server():
    from mcp.server.mcpserver import MCPServer

    player = FormPlayer()
    server = MCPServer("EndPilotActionForms")
    server.tool()(player.connect)
    server.tool()(player.watch_form)
    server.tool()(player.send_chat)
    server.tool()(player.read_form)
    server.tool()(player.answer_form)
    server.tool()(player.disconnect)
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
