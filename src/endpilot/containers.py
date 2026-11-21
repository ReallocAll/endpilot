"""Block container interaction and authoritative window-open observation."""

import asyncio
from dataclasses import dataclass

from .inventory import _response_item


_FACE_OFFSETS = {
    0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1),
    3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0),
}


@dataclass(frozen=True, slots=True)
class ContainerWindow:
    container_id: int
    container_type: str
    position: tuple[int, int, int] | None  # Absent in UpdateTradePacket.
    entity_unique_id: int


_FURNACE_SLOTS = (
    "FURNACE_INGREDIENT_CONTAINER",
    "FURNACE_FUEL_CONTAINER",
    "FURNACE_RESULT_CONTAINER",
)


def _container_slot_name(window: ContainerWindow, slot: int) -> str:
    if window.container_type == "FURNACE":
        if not 0 <= slot < len(_FURNACE_SLOTS):
            raise ValueError("furnace slots are 0=ingredient, 1=fuel, 2=result")
        return _FURNACE_SLOTS[slot]
    return "LEVEL_ENTITY_CONTAINER"


class Containers:
    def __init__(self, packets, movement, inventory):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self.window: ContainerWindow | None = None
        self._slots: dict[int, object] | None = None
        self._slots_updated = asyncio.Event()
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _decode_open(self, raw) -> ContainerWindow:
        packet = self.packets.protocol.decode(raw)
        return ContainerWindow(
            int(packet.container_id),
            packet.type.name if hasattr(packet.type, "name") else str(int(packet.type)),
            (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)),
            int(packet.entity_unique_id),
        )

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.ContainerOpenPacket.ID:
            self.window = self._decode_open(raw)
            self._slots = None
            self._slots_updated.set()
        elif raw.packet_id == b.UpdateTradePacket.ID:
            packet = self.packets.protocol.decode(raw)
            window = ContainerWindow(
                int(packet.container_id), "TRADE", None,
                int(packet.entity_unique_id),
            )
            # Do not replace a matching ContainerOpen window's actual position
            # or initial slots when its server quote arrives or refreshes.
            current = self.window
            if (current is None or current.container_id != window.container_id
                    or current.container_type != "TRADE"
                    or current.entity_unique_id != window.entity_unique_id):
                self.window = window
                self._slots = None
                self._slots_updated.set()
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if self.window is not None and int(packet.inventory_id) == self.window.container_id:
                self._slots = dict(enumerate(packet.slots))
                self._slots_updated.set()
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if (self.window is not None and self._slots is not None
                    and int(packet.inventory_id) == self.window.container_id):
                self._slots[int(packet.slot)] = packet.item
                self._slots_updated.set()
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if (self.window is not None and self._slots is not None
                        and source.type.name == "CONTAINER_INVENTORY"
                        and source.container_id is not None
                        and int(source.container_id) == self.window.container_id):
                    self._slots[int(action.slot)] = action.to_item_descriptor
                    self._slots_updated.set()
        elif raw.packet_id == b.ContainerClosePacket.ID:
            packet = self.packets.protocol.decode(raw)
            if self.window is not None and int(packet.container_id) == self.window.container_id:
                self.window = None
                self._slots = None
                self._slots_updated.set()
                if packet.server_initiated_close:
                    # A server-originated close is acknowledged with the same
                    # window ID/type and flag, as in EndstoneMC/client.
                    asyncio.create_task(self.packets.send(b.ContainerClosePacket(
                        container_id=packet.container_id,
                        container_type=packet.container_type,
                        server_initiated_close=True,
                    )))

    @property
    def slots(self) -> dict[int, object] | None:
        """Last server-supplied slots for the active window, or None before its content."""
        return None if self._slots is None else dict(self._slots)

    async def wait_for_slots(self, *, timeout: float = 15) -> dict[int, object]:
        """Wait for an initial InventoryContent for this window, not a client prediction."""
        window = self.window
        if window is None:
            raise RuntimeError("no container window is open")
        async with asyncio.timeout(timeout):
            while self.window is window:
                if self._slots is not None:
                    return dict(self._slots)
                self._slots_updated.clear()
                await self._slots_updated.wait()
        raise RuntimeError("container window closed or changed before its contents arrived")

    async def open(self, position: tuple[int, int, int], blocks, *, face: int = 1,
                   timeout: float = 15) -> ContainerWindow:
        """Return only after BDS sends ContainerOpenPacket for the clicked block."""
        b = self.packets.protocol.binding
        x, y, z = await self.movement.wait_for_position_sync(timeout)
        observed = blocks.observed_block(position)
        runtime_id = 0 if observed is None or observed.runtime_id is None else observed.runtime_id
        px, py, pz = position
        dx, dy, dz = _FACE_OFFSETS.get(face, (0, 0, 0))
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz),
            face=face,
            slot=self.inventory.selected_slot,
            hand=b.HandSlot.MAINHAND,
            item=self.inventory.item_or_empty(self.inventory.selected_slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.5 + dx * 0.5, y=0.5 + dy * 0.5, z=0.5 + dz * 0.5),
            target_block_id=runtime_id,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)) == position

        # Subscribe before submission: a local BDS can open the window in the same batch.
        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ContainerOpenPacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.InventoryTransactionPacket(
                legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
                legacy_set_item_slots=None,
                variant_transaction=transaction,
            ))
            return self._decode_open(await pending)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def open_inventory_screen(self, *, timeout: float = 15) -> ContainerWindow:
        """Open the player inventory UI and await BDS's ContainerOpen response."""
        b = self.packets.protocol.binding
        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ContainerOpenPacket.ID, timeout=timeout, match=lambda raw: True,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.InteractPacket(
                action=b.InteractPacket.Action.OPEN_INVENTORY,
                target_id=self.inventory.player_id,
                pos=None,
            ))
            return self._decode_open(await pending)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def wait_for_close(self, *, timeout: float = 15):
        """Await the active window's server close packet, without requesting a close.

        Register the waiter before the server action. Returns a typed
        ContainerClosePacket, including its server_initiated_close flag.
        """
        window = self.window
        if window is None:
            raise RuntimeError("no container window is open")
        b = self.packets.protocol.binding

        def matches(raw):
            return int(self.packets.protocol.decode(raw).container_id) == window.container_id

        raw = await self.packets.wait_for_packet(
            b.ContainerClosePacket.ID, timeout=timeout, match=matches,
        )
        return self.packets.protocol.decode(raw)

    async def close_window(self, *, timeout: float = 15) -> ContainerWindow | None:
        """Request close; return after the same window's server close packet."""
        window = self.window
        if window is None:
            return None
        b = self.packets.protocol.binding

        pending = asyncio.create_task(self.wait_for_close(timeout=timeout))
        try:
            # Register the observer before sending; BDS may reply immediately.
            await asyncio.sleep(0)
            container_type = (
                int(window.container_type) if window.container_type.lstrip("-").isdigit()
                else b.ContainerType[window.container_type]
            )
            await self.packets.send(b.ContainerClosePacket(
                container_id=window.container_id,
                container_type=container_type,
                server_initiated_close=False,
            ))
            await pending
            return window
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def take_from_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Take an observed container item; return the matching server verdict."""
        return await self._transfer(
            source_slot, destination_slot, amount, into_container=False, timeout=timeout
        )

    async def put_into_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Place an item into the open container; return the matching server verdict."""
        return await self._transfer(
            source_slot, destination_slot, amount, into_container=True, timeout=timeout
        )

    async def _transfer(
        self, source_slot: int, destination_slot: int, amount: int,
        *, into_container: bool, timeout: float,
    ):
        window = self.window
        if window is None or self._slots is None:
            raise RuntimeError("container window or its server-reported slots are unavailable")
        if not 0 <= source_slot <= 255 or not 0 <= destination_slot <= 255:
            raise ValueError("item-stack request slots must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("item-stack request amount must be between 1 and 255")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        player_name = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
        if into_container:
            source = self.inventory.item_or_empty(source_slot)
            destination = self._slots.get(destination_slot) or self.inventory.item_or_empty(-1)
            source_name, destination_name = player_name, _container_slot_name(window, destination_slot)
            action_type = cereal.PlaceActionData
            chest_slot, player_slot = destination_slot, source_slot
        else:
            source = self._slots.get(source_slot)
            destination = self.inventory.item_or_empty(destination_slot)
            source_name, destination_name = _container_slot_name(window, source_slot), player_name
            action_type = cereal.TakeActionData
            chest_slot, player_slot = source_slot, destination_slot
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot,
                net_id_variant=int(item.net_id_variant or 0),
            )

        request_id = self.inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action_type(
                amount=amount,
                source=slot_info(source_name, source_slot, source),
                destination=slot_info(destination_name, destination_slot, destination),
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(
                int(reply.client_request_id.raw_id) == request_id
                for reply in packet.responses
            )

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            response = next(
                reply for reply in packet.responses
                if int(reply.client_request_id.raw_id) == request_id
            )
            # A reply can correct other slots as well as the action's endpoints.
            # The response carries amounts/net IDs, not item types: unknown
            # nonempty slots stay unknown until an inventory packet identifies them.
            container_name = _container_slot_name(window, chest_slot)
            empty = self.inventory.item_or_empty(-1)
            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == container_name and self.window is window and self._slots is not None:
                        previous = self._slots.get(index) or empty
                        fallback = source if index == chest_slot else previous
                        if int(slot.amount) > 0 and int(previous.id) == int(fallback.id) == 0:
                            self._slots.pop(index, None)
                        else:
                            self._slots[index] = _response_item(b, previous, fallback, slot)
                        self._slots_updated.set()
                    elif name in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER", player_name):
                        previous = self.inventory.item_or_empty(index)
                        fallback = source if index == player_slot else previous
                        updated = (
                            None if int(slot.amount) > 0 and int(previous.id) == int(fallback.id) == 0
                            else _response_item(b, previous, fallback, slot)
                        )
                        self.inventory._set_slot(index, updated)
            return response
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def close(self) -> None:
        self._unsubscribe()
