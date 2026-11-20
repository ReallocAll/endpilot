"""Observe server actors and dropped items on this player's packet stream."""

import asyncio
from dataclasses import dataclass, replace


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


@dataclass(frozen=True, slots=True)
class EntityState:
    """Latest observed actor state; item fields are populated for dropped items."""

    entity_id: int
    runtime_id: int
    actor_type: str
    position: tuple[float, float, float]
    item_id: str | None = None
    item_count: int | None = None
    item_network_id: int | None = None


class Entities:
    def __init__(self, packets, *, item_names: dict[int, str] | None = None):
        self.packets = packets
        self._item_names = dict(item_names or {})
        self._by_runtime: dict[int, EntityState] = {}
        self._runtime_by_id: dict[int, int] = {}
        self._unsubscribe = None

    def start(self) -> None:
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def snapshot(self) -> tuple[EntityState, ...]:
        """Snapshot actors observed since subscription; not a world entity scan."""
        return tuple(self._by_runtime.values())

    def get(self, runtime_id: int) -> EntityState | None:
        return self._by_runtime.get(runtime_id)

    def drops(self) -> tuple[EntityState, ...]:
        return tuple(state for state in self._by_runtime.values()
                     if state.actor_type == "minecraft:item")

    def _record(self, state: EntityState) -> None:
        previous = self._by_runtime.get(state.runtime_id)
        if previous is not None:
            self._runtime_by_id.pop(previous.entity_id, None)
        previous_runtime = self._runtime_by_id.get(state.entity_id)
        if previous_runtime is not None and previous_runtime != state.runtime_id:
            self._by_runtime.pop(previous_runtime, None)
        self._by_runtime[state.runtime_id] = state
        self._runtime_by_id[state.entity_id] = state.runtime_id

    def _item_state(self, packet) -> EntityState:
        item = packet.item
        network_id = int(item.id)
        return EntityState(
            int(packet.id), int(packet.runtime_id), "minecraft:item",
            (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
            self._item_names.get(network_id, f"#{network_id}"),
            int(item.stack_size), network_id,
        )

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.ItemRegistryPacket.ID:
            packet = self.packets.protocol.decode(raw)
            self._item_names = {int(item.id): str(item.name) for item in packet.items}
        elif raw.packet_id == b.AddActorPacket.ID:
            actor = self.packets.protocol.decode(raw)
            self._record(EntityState(
                int(actor.entity_id), int(actor.runtime_id), actor.actor_type,
                (float(actor.pos.x), float(actor.pos.y), float(actor.pos.z)),
            ))
        elif raw.packet_id == b.AddItemActorPacket.ID:
            self._record(self._item_state(self.packets.protocol.decode(raw)))
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            # Actors from the previous dimension are no longer observable.
            self._by_runtime.clear()
            self._runtime_by_id.clear()
        elif raw.packet_id == b.RemoveActorPacket.ID:
            entity_id = int(self.packets.protocol.decode(raw).entity_id)
            runtime_id = self._runtime_by_id.pop(entity_id, None)
            if runtime_id is not None:
                self._by_runtime.pop(runtime_id, None)
        elif raw.packet_id == b.TakeItemActorPacket.ID:
            runtime_id = int(self.packets.protocol.decode(raw).item_id)
            state = self._by_runtime.pop(runtime_id, None)
            if state is not None:
                self._runtime_by_id.pop(state.entity_id, None)
        elif raw.packet_id in (b.MoveActorAbsolutePacket.ID, b.MoveActorDeltaPacket.ID):
            move = self.packets.protocol.decode(raw).move_data
            runtime_id = int(move.runtime_id)
            state = self._by_runtime.get(runtime_id)
            if state is None:
                return
            if raw.packet_id == b.MoveActorAbsolutePacket.ID:
                position = (float(move.pos.x), float(move.pos.y), float(move.pos.z))
            else:
                position = tuple(
                    float(value) if value is not None else old
                    for value, old in zip(
                        (move.new_position_x, move.new_position_y, move.new_position_z),
                        state.position,
                    )
                )
            self._by_runtime[runtime_id] = replace(state, position=position)

    async def attack(self, movement, inventory, runtime_id: int, *,
                     slot: int = 0, hit_height: float = 0.5) -> None:
        """Submit one attack using the latest observed actor position.

        Uses the same ItemUseOnActor inventory transaction as EndstoneMC/client.
        Completion means only that the packet was submitted, not server damage.
        """
        actor = self.get(runtime_id)
        if actor is None:
            raise ValueError("actor is not currently observed")
        item = await inventory.wait_for_slot(slot)
        x, y, z = await movement.wait_for_position_sync()
        b = self.packets.protocol.binding
        action = b.ItemUseOnActorInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            runtime_id=runtime_id,
            action_type=b.ItemUseOnActorInventoryTransaction.ActionType.ATTACK,
            slot=slot,
            item=item,
            from_pos=b.Vec3(x=x, y=y, z=z),
            hit_pos=b.Vec3(
                x=actor.position[0],
                y=actor.position[1] + hit_height,
                z=actor.position[2],
            ),
        )
        await self.packets.send(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None,
            variant_transaction=action,
        ))

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

    async def wait_for_item_pickup(
        self, runtime_id: int, player_id: int, *, timeout: float = 15
    ) -> None:
        """Wait for BDS to name this player as the collector of this item actor."""
        b = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return int(packet.item_id) == runtime_id and int(packet.actor_id) == player_id

        await self.packets.wait_for_packet(
            b.TakeItemActorPacket.ID, match=matches, timeout=timeout
        )

    async def collect_drop(
        self, movement, runtime_id: int, player_id: int, *,
        ticks: int = 120, timeout: float = 15
    ) -> EntityState:
        """Approach an observed drop; return only after BDS confirms our pickup.

        Movement uses a straight path on level terrain; the item may be taken by
        another actor, despawn, or remain unreachable. Those do not imply success.
        """
        drop = self.get(runtime_id)
        if drop is None or drop.actor_type != "minecraft:item":
            raise ValueError("item actor is not currently observed")
        pickup = asyncio.create_task(self.wait_for_item_pickup(
            runtime_id, player_id, timeout=timeout
        ))
        follow = None
        try:
            # Register the packet waiter before sending any movement input.
            await asyncio.sleep(0)
            follow = asyncio.create_task(movement.follow_entity(
                self, runtime_id, ticks=ticks, stop_distance=0.0
            ))
            done, _ = await asyncio.wait(
                (pickup, follow), return_when=asyncio.FIRST_COMPLETED
            )
            if follow in done:
                await follow
            await pickup
            return drop
        finally:
            tasks = (pickup, follow) if follow is not None else (pickup,)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_item_drop(self, timeout: float = 15) -> EntityState:
        """Observe the next server-sent item spawn, not an inventory transfer."""
        b = self.packets.protocol.binding
        raw = await self.packets.wait_for_packet(b.AddItemActorPacket.ID, timeout=timeout)
        return self._item_state(self.packets.protocol.decode(raw))

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
