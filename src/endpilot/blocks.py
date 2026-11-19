"""Observe authoritative block updates sent to the player, without guessing unloaded terrain."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BlockUpdate:
    position: tuple[int, int, int]
    runtime_id: int
    layer: int


class Blocks:
    def __init__(self, packets):
        self.packets = packets

    async def wait_for_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        """Wait for a *new* UpdateBlock packet, not a snapshot of the world."""
        binding = self.packets.protocol.binding
        future = asyncio.get_running_loop().create_future()

        def observe(raw):
            if raw.packet_id != binding.UpdateBlockPacket.ID:
                return
            packet = self.packets.protocol.decode(raw)
            pos = (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z))
            if pos == position and int(packet.layer) == layer and not future.done():
                future.set_result(BlockUpdate(pos, int(packet.runtime_id), int(packet.layer)))

        unsubscribe = self.packets.subscribe_raw(observe)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()
