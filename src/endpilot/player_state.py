"""Server-reported health and respawn for the local player."""

import asyncio

class PlayerState:
    def __init__(self, packets, runtime_id: int):
        self.packets = packets
        self.runtime_id = int(runtime_id)
        self.health: float | None = None
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _read_health(self, raw) -> float | None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.SetHealthPacket.ID:
            return float(self.packets.protocol.decode(raw).health)
        if raw.packet_id != b.UpdateAttributesPacket.ID:
            return None
        packet = self.packets.protocol.decode(raw)
        if int(packet.runtime_id) != self.runtime_id:
            return None
        for attribute in packet.attribute_data:
            if str(attribute.name) == "minecraft:health":
                return float(attribute.current_value)
        return None

    def _on_packet(self, raw) -> None:
        value = self._read_health(raw)
        if value is not None:
            self.health = value

    async def wait_for_health_update(self, timeout: float = 15) -> float:
        """Return the next server health update, not a locally predicted value."""
        b = self.packets.protocol.binding
        raw = await self.packets.wait_for_packet(
            (b.UpdateAttributesPacket.ID, b.SetHealthPacket.ID),
            timeout=timeout,
            match=lambda packet: self._read_health(packet) is not None,
        )
        return self._read_health(raw)

    async def respawn(self, *, timeout: float = 15) -> tuple[float, float, float]:
        """Press Respawn, await the server's spawn point, then acknowledge it.

        The returned position is the server's READY_TO_SPAWN position, not
        proof that subsequent movement has been accepted.
        """
        b = self.packets.protocol.binding

        def ready(raw):
            packet = self.packets.protocol.decode(raw)
            return (int(packet.runtime_id) == self.runtime_id
                    and packet.state == b.PlayerRespawnState.READY_TO_SPAWN)

        # Register before sending: the server may answer in the very next batch.
        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.RespawnPacket.ID, timeout=timeout, match=ready,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.RespawnPacket(
                pos=b.Vec3(x=0.0, y=0.0, z=0.0),
                state=b.PlayerRespawnState.CLIENT_READY_TO_SPAWN,
                runtime_id=self.runtime_id,
            ))
            response = self.packets.protocol.decode(await pending)
            await self.packets.send(b.PlayerActionPacket(
                runtime_id=self.runtime_id,
                action=b.PlayerActionType.RESPAWN,
                pos=b.BlockPos(x=0, y=0, z=0),
                result_pos=b.BlockPos(x=0, y=0, z=0),
                face=0,
            ))
            return (float(response.pos.x), float(response.pos.y), float(response.pos.z))
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def close(self) -> None:
        self._unsubscribe()
