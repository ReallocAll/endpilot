"""Player-initiated commands and observable BDS text feedback."""

import asyncio
import uuid

from bedrock_protocol import TextPacketType


class Commands:
    def __init__(self, packets, player_uuid: uuid.UUID, player_unique_id: int):
        self.packets = packets
        self.player_uuid = player_uuid
        self.player_unique_id = player_unique_id
        self._lock = asyncio.Lock()

    async def execute(self, command: str, *, timeout: float = 15):
        """Submit a command and return the next typed BDS command-text response.

        BDS 1.26.52 sends TextPacket(TEXT_OBJECT) without a request ID, not a
        CommandOutputPacket. Commands from this player are serialized, but other
        server text can still interleave: inspect the feedback and server effect.
        """
        b = self.packets.protocol.binding
        text = b.TextPacket
        origin = b.CommandOriginData(
            type=b.CommandOriginType.PLAYER,
            uuid=self.player_uuid,
            request_id=uuid.uuid4().hex,
            player_id=self.player_unique_id,
        )
        packet = b.CommandRequestPacket(
            command=command, origin=origin,
            internal_source=False, version=b.CurrentCmdVersion.LATEST,
        )

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
