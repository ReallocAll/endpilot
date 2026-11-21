"""Read server-issued villager quotes after an ordinary actor interaction."""

import asyncio
from dataclasses import dataclass

from .inventory import _response_item


@dataclass(frozen=True, slots=True)
class TradeItem:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class TradeOffer:
    recipe_net_id: int | None
    buy_a: TradeItem | None
    buy_b: TradeItem | None
    sell: TradeItem | None
    uses: int | None
    max_uses: int | None


@dataclass(frozen=True, slots=True)
class TradeOffers:
    window_id: int
    trader_unique_id: int
    display_name: str
    trader_tier: int
    use_new_trade_screen: bool
    offers: tuple[TradeOffer, ...]


def _lookup(compound, key: str):
    # Native CompoundTag supports indexing and membership, but not dict.get.
    return compound[key] if key in compound else None


def _number(compound, key: str) -> int | None:
    value = _lookup(compound, key)
    return None if value is None else int(value.value)


def _item(compound, key: str) -> TradeItem | None:
    value = _lookup(compound, key)
    if value is None:
        return None
    name, count = _lookup(value, "Name"), _lookup(value, "Count")
    if name is None or count is None:
        return None
    return TradeItem(str(name.value), int(count.value))


def _read_offers(packet) -> TradeOffers:
    recipes = _lookup(packet.data, "Recipes")
    offers = tuple(
        TradeOffer(
            _number(recipe, "netId"),
            _item(recipe, "buyA"),
            _item(recipe, "buyB"),
            _item(recipe, "sell"),
            _number(recipe, "uses"),
            _number(recipe, "maxUses"),
        )
        for recipe in (() if recipes is None else recipes)
    )
    return TradeOffers(
        int(packet.container_id), int(packet.entity_unique_id),
        str(packet.display_name), int(packet.trader_tier),
        bool(packet.use_new_trade_screen), offers,
    )


class Trading:
    def __init__(self, packets, entities, movement, inventory, containers, item_names=None):
        self.packets = packets
        self.entities = entities
        self.movement = movement
        self.inventory = inventory
        self.containers = containers
        self.item_names = item_names or {}
        self.offers: TradeOffers | None = None
        self._inputs = {}  # number -> (name, amount, confirmed net ID, original item)

    async def open(self, runtime_id: int, *, timeout: float = 15) -> TradeOffers:
        """Interact and await a matching server UpdateTrade; not a completed exchange."""
        entity = self.entities.get(runtime_id)
        if entity is None:
            raise ValueError("trading requires an observed actor to match the server's unique ID")
        b = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return int(packet.entity_unique_id) == entity.entity_id

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.UpdateTradePacket.ID, match=matches, timeout=timeout,
        ))
        try:
            # Register before submitting: local BDS may answer in the same batch.
            await asyncio.sleep(0)
            await self.entities.interact(self.movement, self.inventory, runtime_id)
            offers = _read_offers(self.packets.protocol.decode(await pending))
            self.offers = offers
            self._inputs.clear()
            return offers
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def stage_ingredient(
        self, source_slot: int, amount: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Move an observed player stack into the open trade input; return BDS's verdict.

        This stages an ingredient only; it does not select or complete a trade.
        """
        offers, window = self.offers, self.containers.window
        if (offers is None or window is None or window.container_type != "TRADE"
                or window.container_id != offers.window_id
                or window.entity_unique_id != offers.trader_unique_id):
            raise RuntimeError("no matching trade window is open")
        if not 0 <= source_slot <= 255:
            raise ValueError("source slot must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        if ingredient not in (1, 2):
            raise ValueError("ingredient must be 1 or 2")
        source = self.inventory.slots.get(source_slot)
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        destination_name = (
            f"TRADE2_INGREDIENT_{ingredient}_CONTAINER"
            if offers.use_new_trade_screen else f"TRADE_INGREDIENT_{ingredient}_CONTAINER"
        )

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot, net_id_variant=net_id,
            )

        request_id = self.inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.PlaceActionData(
                amount=amount,
                source=slot_info(
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    source_slot, int(source.net_id_variant or 0),
                ),
                destination=slot_info(
                    destination_name, ingredient + 3 if offers.use_new_trade_screen else 0, 0
                ),
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
            # The reply carries authoritative counts/net IDs, including corrections
            # on failure. Do not infer the outcome from sending the request.
            for container in response.containers or ():
                if container.full_container_name.name.name not in (
                    "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                ):
                    continue
                for slot in container.slots:
                    if int(slot.slot) == source_slot:
                        current = self.inventory.item_or_empty(source_slot)
                        self.inventory._set_slot(
                            source_slot, _response_item(b, current, source, slot)
                        )
            if response.result.name == "SUCCESS":
                physical_slot = ingredient + 3 if offers.use_new_trade_screen else 0
                self._inputs.pop(ingredient, None)
                for container in response.containers or ():
                    if container.full_container_name.name.name != destination_name:
                        continue
                    for slot in container.slots:
                        if int(slot.slot) != physical_slot:
                            continue
                        net_id = slot.item_stack_net_id
                        name = self.item_names.get(int(source.id))
                        if int(slot.amount) and net_id is not None and name is not None:
                            self._inputs[ingredient] = (
                                name, int(slot.amount), int(net_id.raw_id), source,
                            )
            return response
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def unstage_ingredient(
        self, destination_slot: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Return a server-confirmed trade input to inventory before closing the window.

        BDS 1.26.52 does not refund staged inputs merely because the trade window
        closes. Do not report success until its ItemStackResponse accepts the move.
        """
        offers, window = self.offers, self.containers.window
        if (offers is None or window is None or window.container_type != "TRADE"
                or window.container_id != offers.window_id
                or window.entity_unique_id != offers.trader_unique_id):
            raise RuntimeError("no matching trade window is open")
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        if ingredient not in (1, 2):
            raise ValueError("ingredient must be 1 or 2")
        staged = self._inputs.get(ingredient)
        if staged is None:
            raise RuntimeError("trade ingredient lacks a confirmed staged stack")
        name, amount, net_id, original = staged
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        container_name = (f"TRADE2_INGREDIENT_{ingredient}_CONTAINER"
                          if offers.use_new_trade_screen else
                          f"TRADE_INGREDIENT_{ingredient}_CONTAINER")
        physical_slot = ingredient + 3 if offers.use_new_trade_screen else 0
        destination = self.inventory.item_or_empty(destination_slot)

        def slot_info(container, slot, net):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[container], dynamic_id=None,
                ), slot=slot, net_id_variant=net,
            )

        request_id = self.inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.PlaceActionData(
                amount=amount,
                source=slot_info(container_name, physical_slot, net_id),
                destination=slot_info(
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", destination_slot,
                    int(destination.net_id_variant or 0),
                ),
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(int(reply.client_request_id.raw_id) == request_id
                       for reply in packet.responses)

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            reply = next(reply for reply in packet.responses
                         if int(reply.client_request_id.raw_id) == request_id)
            for container in reply.containers or ():
                cname = container.full_container_name.name.name
                for slot in container.slots:
                    if cname == container_name and int(slot.slot) == physical_slot:
                        net = slot.item_stack_net_id
                        if int(slot.amount) == 0:
                            self._inputs.pop(ingredient, None)
                        elif net is not None:
                            self._inputs[ingredient] = (
                                name, int(slot.amount), int(net.raw_id), original,
                            )
                    elif (cname in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                          and int(slot.slot) == destination_slot):
                        current = self.inventory.item_or_empty(destination_slot)
                        self.inventory._set_slot(
                            destination_slot, _response_item(b, current, original, slot)
                        )
            return reply
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def exchange(self, recipe_net_id: int, destination_slot: int, *, timeout: float = 15):
        """Submit a quoted exchange and return the matching BDS ItemStackResponse.

        Only server-reported response slots update the observed input and output state.
        """
        offers, window = self.offers, self.containers.window
        if (offers is None or window is None or window.container_type != "TRADE"
                or window.container_id != offers.window_id
                or window.entity_unique_id != offers.trader_unique_id):
            raise RuntimeError("no matching trade window is open")
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        offer = next((o for o in offers.offers if o.recipe_net_id == recipe_net_id), None)
        if offer is None or offer.buy_a is None or offer.sell is None:
            raise ValueError("no complete server quote for this recipe net ID")
        if not 1 <= offer.sell.count <= 255:
            raise ValueError("quoted output amount must fit in a byte")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ), slot=slot, net_id_variant=net_id,
            )

        consumes = []
        input_slots = {}
        for number, item in enumerate((offer.buy_a, offer.buy_b), start=1):
            if item is None:
                continue
            staged = self._inputs.get(number)
            if (staged is None or staged[0] != item.name
                    or staged[1] < item.count or staged[2] <= 0):
                raise RuntimeError(f"trade ingredient {number} lacks a confirmed staged stack")
            name = (f"TRADE2_INGREDIENT_{number}_CONTAINER"
                    if offers.use_new_trade_screen else f"TRADE_INGREDIENT_{number}_CONTAINER")
            slot = number + 3 if offers.use_new_trade_screen else 0
            consumes.append(cereal.ConsumeActionData(
                amount=item.count, source=slot_info(name, slot, staged[2]),
            ))
            input_slots[(name, slot)] = (number, staged[0])

        request_id = self.inventory._allocate_request_id()
        destination = self.inventory.item_or_empty(destination_slot)
        output_id = next(
            (id for id, name in self.item_names.items() if name == offer.sell.name), None
        )
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[
                cereal.CraftRecipeActionData(recipe_net_id=recipe_net_id, num_crafts=1),
                cereal.CraftResultsActionData(
                    craft_results=[cereal.NetworkItemInstanceDescriptorData(
                        item_descriptor=cereal.ItemNameDescriptorData(
                            full_name=offer.sell.name, aux_value=0,
                        ),
                        stack_size=offer.sell.count, block_runtime_id=0,
                        user_data_buffer=b"\0" * 10,
                    )], num_crafts=1,
                ),
                *consumes,
                cereal.PlaceActionData(
                    amount=offer.sell.count,
                    source=slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                    destination=slot_info(
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                        destination_slot, int(destination.net_id_variant or 0),
                    ),
                ),
            ],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(int(reply.client_request_id.raw_id) == request_id
                       for reply in packet.responses)

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            reply = next(reply for reply in packet.responses
                         if int(reply.client_request_id.raw_id) == request_id)
            for container in reply.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    input_info = input_slots.get((name, index))
                    if input_info is not None:
                        number, item_name = input_info
                        net_id = slot.item_stack_net_id
                        if int(slot.amount) == 0:
                            self._inputs.pop(number, None)
                        elif net_id is not None:
                            previous = self._inputs[number]
                            self._inputs[number] = (
                                item_name, int(slot.amount), int(net_id.raw_id),
                                previous[3],
                            )
                    elif (name in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                                   "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                          and index == destination_slot):
                        current = self.inventory.item_or_empty(index)
                        if (reply.result.name == "SUCCESS" and output_id is not None
                                and int(current.id) != output_id):
                            current = b.SerializedNetworkItemStackDescriptor(
                                id=output_id, stack_size=offer.sell.count,
                                aux_value=0, net_id_variant=None,
                                block_runtime_id=0, user_data_buffer=b"\0" * 10,
                            )
                        if int(current.id) != 0 or int(slot.amount) == 0:
                            self.inventory._set_slot(
                                index, _response_item(b, current, current, slot)
                            )
            return reply
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
