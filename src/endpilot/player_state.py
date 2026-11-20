"""Server-reported health for the local player."""

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

    def close(self) -> None:
        self._unsubscribe()
