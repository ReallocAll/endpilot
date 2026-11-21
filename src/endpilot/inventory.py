"""Server-reported inventory slots needed by player item actions."""

import asyncio


def _response_item(binding, previous, fallback, response_slot):
    """Apply a server slot correction without inventing a new item identity."""
    amount = int(response_slot.amount)
    if amount == 0:
        return binding.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
    item = previous if int(previous.id) != 0 else fallback
    net_id = response_slot.item_stack_net_id
    return binding.SerializedNetworkItemStackDescriptor(
        id=int(item.id), stack_size=amount, aux_value=int(item.aux_value),
        net_id_variant=int(net_id.raw_id) if net_id is not None else item.net_id_variant,
        block_runtime_id=int(item.block_runtime_id),
        user_data_buffer=item.user_data_buffer,
    )


class Inventory:
    def __init__(self, packets, player_id: int):
        self.packets = packets
        self.player_id = int(player_id)
        self._slots = {}
        self._armor_slots = {}
        self._next_request_id = -1
        self._selected_slot = 0
        self._events = {}
        self._changed = asyncio.Event()
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _set_slot(self, slot, item):
        self._slots[int(slot)] = item
        event = self._events.get(int(slot))
        if event is not None:
            event.set()
        self._changed.set()

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            inventory_id = int(packet.inventory_id)
            if inventory_id == int(b.ContainerID.ARMOR):
                self._armor_slots = dict(enumerate(packet.slots))
                return
            if inventory_id != int(b.ContainerID.INVENTORY):
                return
            self._slots = dict(enumerate(packet.slots))
            for event in self._events.values():
                event.set()
            self._changed.set()
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == int(b.ContainerID.INVENTORY):
                self._set_slot(packet.slot, packet.item)
            elif int(packet.inventory_id) == int(b.ContainerID.ARMOR):
                self._armor_slots[int(packet.slot)] = packet.item
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            # Server-initiated inventory changes, including automatic pickup.
            # Match EndstoneMC/client's container/action reconciliation.
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if source.type.name != "CONTAINER_INVENTORY" or source.container_id is None:
                    continue
                inventory_id = int(source.container_id)
                if inventory_id == int(b.ContainerID.INVENTORY):
                    self._set_slot(action.slot, action.to_item_descriptor)
                elif inventory_id == int(b.ContainerID.ARMOR):
                    self._armor_slots[int(action.slot)] = action.to_item_descriptor

    @property
    def slots(self) -> dict[int, object]:
        """Latest server-described inventory slots; missing slots are unknown."""
        return dict(self._slots)

    @property
    def armor_slots(self) -> dict[int, object]:
        """Latest server-described armor slots; missing slots are not observations."""
        return dict(self._armor_slots)

    def _allocate_request_id(self) -> int:
        # One request ID space for armor and open-container operations.
        request_id = self._next_request_id
        self._next_request_id -= 2
        return request_id

    def count_item(self, item_id: int) -> int:
        """Sum the latest server-described inventory slots for a numeric item ID."""
        return sum(
            int(item.stack_size) for item in self._slots.values()
            if int(item.id) == item_id
        )

    async def wait_for_item_count(self, item_id: int, minimum: int, *, timeout: float = 15) -> int:
        """Wait for an observed inventory count; unrelated slot changes are allowed."""
        async with asyncio.timeout(timeout):
            while True:
                count = self.count_item(item_id)
                if count >= minimum:
                    return count
                self._changed.clear()
                await self._changed.wait()

    @property
    def selected_slot(self) -> int:
        """Last locally selected hotbar slot, not a BDS acceptance receipt."""
        return self._selected_slot

    def item_or_empty(self, slot: int):
        """Return an observed item or the client's initial empty-hand descriptor.

        A fresh BDS connection need not send InventoryContent for empty slots.
        Match EndstoneMC/client's initially empty inventory instead of waiting
        for inventory state that an unarmed attack does not require.
        """
        b = self.packets.protocol.binding
        return self._slots.get(slot) or b.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )

    async def wait_for_item(self, slot: int, timeout: float = 15):
        """Return the current server-reported nonempty inventory slot."""
        event = self._events.setdefault(slot, asyncio.Event())
        async with asyncio.timeout(timeout):
            while True:
                item = self._slots.get(slot)
                if item is not None and int(item.id) != 0:
                    return item
                event.clear()
                await event.wait()

    async def select_hotbar(self, slot: int) -> None:
        """Request a selected hotbar slot; only BDS can confirm the held item."""
        if not 0 <= slot < 9:
            raise ValueError("hotbar slot must be between 0 and 8")
        b = self.packets.protocol.binding
        # BDS 1.26.52 accepts this even though EndstoneMC/client uses
        # MobEquipment; that packet alone did not switch the server's slot.
        await self.packets.send(b.PlayerHotbarPacket(
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
            should_select_slot=True,
        ))
        self._selected_slot = slot

    async def announce_equipment(self, slot: int, item) -> None:
        """Carry the server-observed item descriptor for a use transaction."""
        b = self.packets.protocol.binding
        await self.packets.send(b.MobEquipmentPacket(
            runtime_id=self.player_id,
            item=item,
            slot=slot,
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
        ))
        self._selected_slot = slot



    async def equip_armor(self, source_slot: int, armor_slot: int, *, timeout: float = 15):
        """Swap a player inventory item with armor slot 0=head, 1=chest, 2=legs, 3=feet.

        Return the matching ItemStackResponse; only server-provided response slots
        (or later inventory packets) update the observed inventory and armor state.
        """
        if not 0 <= source_slot <= 255:
            raise ValueError("source slot must fit in a byte")
        if not 0 <= armor_slot < 4:
            raise ValueError("armor slot must be between 0 and 3")
        source = self._slots.get(source_slot)
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")

        b = self.packets.protocol.binding
        destination = self._armor_slots.get(armor_slot) or self.item_or_empty(-1)
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot,
                net_id_variant=int(item.net_id_variant or 0),
            )

        source_info = slot_info(
            "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", source_slot, source,
        )
        destination_info = slot_info("ARMOR_CONTAINER", armor_slot, destination)
        # Match client item-stack actions: place into an empty armor slot,
        # swap when replacing an already equipped item.
        placing = int(destination.id) == 0
        action = (
            cereal.PlaceActionData(
                amount=1, source=source_info, destination=destination_info,
            )
            if placing else cereal.SwapActionData(
                source=source_info, destination=destination_info,
            )
        )
        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action],
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
            success = response.result.name == "SUCCESS"
            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == "ARMOR_CONTAINER" and index == armor_slot:
                        item = source if success else destination
                        if not success and int(item.id) == 0 and int(slot.amount) > 0:
                            # Response slots have no item type: do not invent one.
                            self._armor_slots.pop(index, None)
                        else:
                            self._armor_slots[index] = _response_item(b, item, item, slot)
                    elif name in (
                        "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    ) and index == source_slot:
                        item = (source if placing else destination) if success else source
                        self._set_slot(index, _response_item(b, item, item, slot))
            return response
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def use_item(self, movement) -> None:
        """Submit one use-in-air transaction; server effects are observed separately."""
        x, y, z = await movement.wait_for_position_sync()
        b = self.packets.protocol.binding
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.USE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=0, y=0, z=0),
            face=255,
            slot=self.selected_slot,
            hand=b.HandSlot.MAINHAND,
            item=self.item_or_empty(self.selected_slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.0, y=0.0, z=0.0),
            target_block_id=0,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        # Match EndstoneMC/client GameMode.use_item: an independent transaction,
        # not a PlayerAuthInput block-placement action.
        await self.packets.send(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None,
            variant_transaction=transaction,
        ))

    def close(self):
        self._unsubscribe()
