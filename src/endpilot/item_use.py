"""Submit hand actions and observe the server's held-item lifecycle."""

import asyncio
from dataclasses import dataclass
from typing import Literal


# SetActorData index 0 carries the low actor-flag bits in BDS 2225.
_ACTOR_FLAGS = 0
_CONSUME_METHODS: dict[int, Literal["eat", "consume"]] = {1: "eat", 3: "consume"}


@dataclass(frozen=True, slots=True)
class ItemConsumption:
    """Server-confirmed outcome in the selected slot, including a returned container."""

    slot: int
    used_item_id: int
    method: Literal["eat", "consume"]
    held_item_id: int
    held_count: int


@dataclass(frozen=True, slots=True)
class ItemCooldownEvent:
    """An item cooldown category and duration announced by the server."""

    category: str
    duration_ticks: int


def _stack(item) -> tuple[int, int, int, bytes]:
    return int(item.id), int(item.aux_value), int(item.stack_size), bytes(item.user_data_buffer)


class ItemUse:
    def __init__(self, packets, movement, inventory, runtime_id: int):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self.runtime_id = runtime_id
        self.using_item: bool | None = None
        self._consuming = False
        self._disconnected = False
        b = packets.protocol.binding
        self._interruptions = (b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID, b.RespawnPacket.ID)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.using_item = None

    def _interrupts(self, raw) -> bool:
        protocol = self.packets.protocol
        if raw.packet_id == protocol.binding.RespawnPacket.ID:
            # BDS can address its clientbound local respawn as runtime ID 0.
            return protocol.decode(raw).runtime_id in (0, self.runtime_id)
        return raw.packet_id in self._interruptions

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self._disconnected = True
        if self._interrupts(raw):
            self.using_item = None
        elif raw.packet_id == b.SetActorDataPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.id != self.runtime_id:
                return
            for entry in packet.packed_items.data:
                if entry.id == _ACTOR_FLAGS and isinstance(entry.payload, b.DataItemInt64Payload):
                    self.using_item = bool(entry.payload.value & (1 << int(b.ActorFlags.USINGITEM)))
                    return

    async def wait_for_cooldown(
        self, *, category: str | None = None, timeout: float | None = 15,
    ) -> ItemCooldownEvent:
        """Await the next matching announcement and return its server-sent duration."""
        if self._unsubscribe is None or self._disconnected:
            raise ConnectionError("item use session is closed")
        protocol = self.packets.protocol
        b = protocol.binding
        event = None

        def matches(raw):
            nonlocal event
            if raw.packet_id == b.DisconnectPacket.ID or self._unsubscribe is None:
                raise ConnectionError("item use session closed while waiting for a cooldown")
            packet = protocol.decode(raw)
            if category is not None and packet.item_category != category:
                return False
            event = ItemCooldownEvent(packet.item_category, int(packet.duration_ticks))
            return True

        await self.packets.wait_for_packet(
            (b.PlayerStartItemCooldownPacket.ID, b.DisconnectPacket.ID), match=matches, timeout=timeout,
        )
        assert event is not None
        return event

    async def submit(self, *, before_send=None) -> None:
        if self._consuming:
            raise RuntimeError("a consumption is already awaiting confirmation")
        if before_send is None:
            await self.inventory.use_item(self.movement)
        else:
            await self.inventory.use_item(self.movement, before_send=before_send)

    async def release(self) -> None:
        x, y, z = await self.movement.wait_for_position_sync()
        protocol = self.packets.protocol
        b = protocol.binding
        slot = self.inventory.selected_slot
        hand = {"hand": b.HandSlot.MAINHAND} if protocol.number >= 2225 else {}
        transaction = b.ItemReleaseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemReleaseInventoryTransaction.ActionType.RELEASE,
            slot=slot, item=self.inventory.item_or_empty(slot), from_pos=b.Vec3(x=x, y=y, z=z), **hand,
        )
        await self.packets.send(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None, variant_transaction=transaction,
        ))

    async def consume(self, *, timeout: float) -> ItemConsumption:
        async with asyncio.timeout(timeout):
            if self._consuming or self.using_item:
                raise RuntimeError("player is already using a held item")
            self._consuming = True
            try:
                return await self._consume()
            finally:
                self._consuming = False

    async def _consume(self) -> ItemConsumption:
        slot = self.inventory.selected_slot
        selection_revision = self.inventory.selection_revision
        before = _stack(self.inventory.item_or_empty(slot))
        item_id, aux, count, user_data = before
        if item_id == 0 or count <= 0:
            raise LookupError("no server-observed item in the selected hotbar slot")
        await self.movement.wait_for_position_sync(timeout=None)
        protocol = self.packets.protocol
        b = protocol.binding
        submitted = False
        method = None
        result = None

        def matches(raw):
            nonlocal method, result
            if raw.packet_id in self._interruptions:
                if self._interrupts(raw):
                    raise ConnectionError("player session changed while awaiting consumption")
                return False
            if not submitted:
                return False
            if self.inventory.selection_revision != selection_revision:
                raise RuntimeError("selected slot changed while awaiting consumption")
            if raw.packet_id == b.CompletedUsingItemPacket.ID:
                packet = protocol.decode(raw)
                if packet.item_id == item_id and self.using_item:
                    method = _CONSUME_METHODS.get(packet.item_use_method)
                return False
            if method is None:
                return False

            packet = protocol.decode(raw)
            transaction = packet.variant_transaction
            if (not isinstance(transaction, b.ItemReleaseInventoryTransaction)
                    or transaction.action_type != b.ItemReleaseInventoryTransaction.ActionType.USE
                    or transaction.slot != slot or getattr(transaction, "hand", None) != b.HandSlot.MAINHAND):
                return False
            for action in transaction.transaction.actions:
                if (action.slot != slot or action.source.type != b.InventorySourceType.CONTAINER_INVENTORY
                        or action.source.container_id != b.ContainerID.INVENTORY
                        or _stack(action.from_item_descriptor) != before):
                    continue
                held_id, held_aux, held_count, held_data = _stack(action.to_item_descriptor)
                if held_id == item_id:
                    if (held_aux, held_count, held_data) != (aux, count - 1, user_data):
                        continue
                elif count != 1:
                    continue
                result = ItemConsumption(slot, item_id, method, held_id, held_count)
                return True
            return False

        ids = (b.CompletedUsingItemPacket.ID, b.InventoryTransactionPacket.ID, *self._interruptions)
        pending = asyncio.create_task(self.packets.wait_for_packet(ids, timeout=None, match=matches))
        try:
            await asyncio.sleep(0)
            if (self.using_item or self.inventory.selection_revision != selection_revision
                    or _stack(self.inventory.item_or_empty(slot)) != before):
                raise RuntimeError("held item changed before consumption was submitted")
            submitted = True
            await self.inventory.use_item(self.movement)
            await pending
            assert result is not None
            return result
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
