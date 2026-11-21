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
    def __init__(self, packets, entities, movement, inventory, containers):
        self.packets = packets
        self.entities = entities
        self.movement = movement
        self.inventory = inventory
        self.containers = containers
        self.offers: TradeOffers | None = None

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
            return response
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
