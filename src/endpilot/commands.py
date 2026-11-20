"""Player-initiated commands and their matching server responses."""

import asyncio
import uuid


class Commands:
    def __init__(self, packets, player_uuid: uuid.UUID, player_unique_id: int):
        self.packets = packets
        self.player_uuid = player_uuid
        self.player_unique_id = player_unique_id

    async def execute(self, command: str, *, timeout: float = 15):
        """Return the typed CommandOutputPacket for this request, not merely its submission."""
        b = self.packets.protocol.binding
        request_id = uuid.uuid4().hex
        origin = b.CommandOriginData(
            type=b.CommandOriginType.PLAYER,
            uuid=self.player_uuid,
            request_id=request_id,
            player_id=self.player_unique_id,
        )
        packet = b.CommandRequestPacket(
            command=command,
            origin=origin,
            internal_source=False,
            version=b.CurrentCmdVersion.LATEST,
        )

        def matches(raw):
            received = self.packets.protocol.decode(raw)
            return received.origin_data.request_id == request_id

        # Register the response observer before sending, so a fast BDS reply is not lost.
        waiting = asyncio.create_task(self.packets.wait_for_packet(
            b.CommandOutputPacket.ID, match=matches, timeout=timeout,
        ))
        await asyncio.sleep(0)
        try:
            await self.packets.send(packet)
            return self.packets.protocol.decode(await waiting)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
