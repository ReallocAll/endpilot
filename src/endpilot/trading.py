"""Read server-issued villager quotes after an ordinary actor interaction."""

import asyncio
from dataclasses import dataclass


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
        str(packet.display_name), int(packet.trader_tier), offers,
    )


class Trading:
    def __init__(self, packets, entities, movement, inventory):
        self.packets = packets
        self.entities = entities
        self.movement = movement
        self.inventory = inventory

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
            return _read_offers(self.packets.protocol.decode(await pending))
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
