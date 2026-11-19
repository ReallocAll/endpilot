"""Track the player's server-reported position and movement corrections."""

import asyncio


class Movement:
    def __init__(self, packets, player_id):
        self.packets = packets
        self.player_id = int(player_id)
        self.position: tuple[float, float, float] | None = None
        self._updated = asyncio.Event()
        self._unsubscribe = None

    def start(self):
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self.packets.start_receiving()

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.MovePlayerPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.player_id) != self.player_id:
                return
        elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.prediction_type) != 0:
                return
        elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
            packet = self.packets.protocol.decode(raw)
        else:
            return
        self.position = (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z))
        self._updated.set()

    async def wait_for_position_sync(self, timeout: float = 15) -> tuple[float, float, float]:
        """Wait for the first server position; StartGame may contain a placeholder."""
        async with asyncio.timeout(timeout):
            await self._updated.wait()
        return self.position

    def close(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
