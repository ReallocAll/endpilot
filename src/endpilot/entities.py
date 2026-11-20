"""Observe server actor spawn and removal packets on the active player stream."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntitySpawn:
    entity_id: int
    runtime_id: int
    actor_type: str
    position: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class EntityMovement:
    """Coordinates sent in one server movement packet; None means axis omitted."""

    runtime_id: int
    x: float | None
    y: float | None
    z: float | None


class Entities:
    def __init__(self, packets):
        self.packets = packets

    async def wait_for_spawn(self, *, actor_type: str | None = None, timeout: float = 15) -> EntitySpawn:
        """Wait for a new AddActor; an entity already in view will not be replayed."""
        b = self.packets.protocol.binding

        def matches(raw):
            return actor_type is None or self.packets.protocol.decode(raw).actor_type == actor_type

        raw = await self.packets.wait_for_packet(
            b.AddActorPacket.ID, match=matches, timeout=timeout
        )
        packet = self.packets.protocol.decode(raw)
        return EntitySpawn(
            int(packet.entity_id),
            int(packet.runtime_id),
            packet.actor_type,
            (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
        )

    async def wait_for_removal(self, entity_id: int, *, timeout: float = 15) -> None:
        """Wait for a new RemoveActor referencing the server's unique actor ID."""
        b = self.packets.protocol.binding

        def matches(raw):
            return int(self.packets.protocol.decode(raw).entity_id) == entity_id

        await self.packets.wait_for_packet(
            b.RemoveActorPacket.ID, match=matches, timeout=timeout
        )

    async def wait_for_movement(self, runtime_id: int, *, timeout: float = 15) -> EntityMovement:
        """Observe the next movement packet for a specific actor runtime ID.

        Delta packets omit unchanged axes; this is not a full position snapshot.
        """
        b = self.packets.protocol.binding

        def matches(raw):
            return int(self.packets.protocol.decode(raw).move_data.runtime_id) == runtime_id

        raw = await self.packets.wait_for_packet(
            (b.MoveActorAbsolutePacket.ID, b.MoveActorDeltaPacket.ID),
            match=matches, timeout=timeout,
        )
        move = self.packets.protocol.decode(raw).move_data
        if raw.packet_id == b.MoveActorAbsolutePacket.ID:
            return EntityMovement(
                runtime_id, float(move.pos.x), float(move.pos.y), float(move.pos.z)
            )
        return EntityMovement(
            runtime_id,
            float(move.new_position_x) if move.new_position_x is not None else None,
            float(move.new_position_y) if move.new_position_y is not None else None,
            float(move.new_position_z) if move.new_position_z is not None else None,
        )
