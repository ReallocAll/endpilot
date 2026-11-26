"""The local player's server-directed dimension change and loading exchange."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DimensionTransition:
    dimension_id: int
    position: tuple[float, float, float]
    respawn: bool
    loading_screen_id: int | None


class _DimensionTransitions:
    def __init__(self, packets):
        self._packets = packets
        self._binding = binding = packets.protocol.binding
        self.packet_ids = (binding.ChangeDimensionPacket.ID, binding.PlayerActionPacket.ID,
                           binding.DisconnectPacket.ID)
        self.active: DimensionTransition | None = None
        self._closed = False
        self._waiters: set[asyncio.Future[DimensionTransition]] = set()

    async def receive(self, raw, player_id):
        if self._closed:
            return
        b = self._binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self.close()
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            packet = self._packets.protocol.decode(raw)
            pos = packet.pos
            transition = DimensionTransition(
                int(packet.dimension_id), (float(pos.x), float(pos.y), float(pos.z)),
                bool(packet.respawn), packet.loading_screen_id,
            )
            self.active = transition
            await self._send_screen(b.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN, transition)
        elif self.active is not None and raw.packet_id == b.PlayerActionPacket.ID:
            packet = self._packets.protocol.decode(raw)
            if packet.runtime_id != player_id or packet.action != b.PlayerActionType.CHANGE_DIMENSION_ACK:
                return
            # The ready action has no screen ID. It closes the current screen in wire order.
            transition = self.active
            await self._send_screen(b.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN, transition)
            if self._closed:
                return
            await self._packets.send(b.PlayerActionPacket(
                runtime_id=player_id, action=b.PlayerActionType.CHANGE_DIMENSION_ACK,
                pos=b.BlockPos(), result_pos=b.BlockPos(), face=0,
            ))
            self.active = None
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_result(transition)

    async def _send_screen(self, phase, transition):
        if transition.loading_screen_id is None:
            return
        await self._packets.send(self._binding.ServerboundLoadingScreenPacket(
            serverbound_loading_screen_packet_type=phase, loading_screen_id=transition.loading_screen_id,
        ))

    async def wait(self, timeout):
        if self._closed:
            raise ConnectionError("dimension transition stream is closed")
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            async with asyncio.timeout(timeout):
                return await waiter
        finally:
            self._waiters.discard(waiter)

    def close(self, cause=None):
        self._closed = True
        self.active = None
        for waiter in self._waiters:
            if not waiter.done():
                error = ConnectionError("dimension transition stream is closed")
                error.__cause__ = cause
                waiter.set_exception(error)
