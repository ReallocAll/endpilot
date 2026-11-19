"""Track the player's server-reported position and movement corrections."""

import asyncio


class Movement:
    def __init__(self, packets, start_game):
        self.packets = packets
        self.player_id = int(start_game.runtime_id)
        self.position = (float(start_game.pos.x), float(start_game.pos.y), float(start_game.pos.z))
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

    async def wait_for_position_update(self, timeout: float = 15) -> tuple[float, float, float]:
        """Wait until the server sends a movement correction, not until input is submitted."""
        async with asyncio.timeout(timeout):
            await self._updated.wait()
        return self.position

    def close(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
