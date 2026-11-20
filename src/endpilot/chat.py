"""Player chat send and server-relayed player chat observations."""

from dataclasses import dataclass

from bedrock_protocol import TextPacketType


@dataclass(frozen=True, slots=True)
class ChatMessage:
    author: str
    message: str


class Chat:
    def __init__(self, packets, name: str):
        self.packets = packets
        self.name = name

    async def send(self, message: str) -> None:
        """Send a chat packet; this does not confirm BDS relayed it."""
        text = self.packets.protocol.binding.packets[9]
        packet = text(
            localize=False,
            body=text.AuthorAndMessage(type=TextPacketType.CHAT, author=self.name, message=message),
            xuid="",
            platform_id="",
            filtered_message=None,
        )
        await self.packets.send(packet)

    async def wait_for_message(self, *, author: str | None = None, timeout: float = 15) -> ChatMessage:
        """Wait for the next server-relayed player chat; ignore system text and other authors."""
        text = self.packets.protocol.binding.packets[9]

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            body = packet.body
            return (
                isinstance(body, text.AuthorAndMessage)
                and body.type == TextPacketType.CHAT
                and (author is None or body.author == author)
            )

        raw = await self.packets.wait_for_packet(text.ID, match=matches, timeout=timeout)
        body = self.packets.protocol.decode(raw).body
        return ChatMessage(body.author, body.message)
