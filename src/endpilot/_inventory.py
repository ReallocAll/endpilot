from collections import Counter
from dataclasses import dataclass, replace
from types import MappingProxyType

import bedrock_protocol as bp

from ._projection import unwrap
from .observations import InventorySnapshot, TradeOffers, _stack_id
from .types import InventoryArea, TradeItem, TradeOffer, TradeOfferRef

PLAIN_DATA = b"\0" * 10


@dataclass(frozen=True, slots=True)
class Change:
    slot: object
    physical: tuple[int, int]
    before: object
    expected: object


def resized(p, item, count, net_id=...):
    if not count:
        return p.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None, block_runtime_id=0, user_data_buffer=b""
        )
    if net_id is ...:
        net_id = item.net_id_variant
    elif net_id is not None and p is bp.v1001:
        net_id = bp.Variant(0, p.ItemStackNetId(raw_id=net_id))
    return p.SerializedNetworkItemStackDescriptor(
        id=item.id,
        stack_size=count,
        aux_value=item.aux_value,
        net_id_variant=net_id,
        block_runtime_id=item.block_runtime_id,
        user_data_buffer=item.user_data_buffer,
    )


def same_item(a, b):
    return (a.id, a.aux_value, a.block_runtime_id, bytes(a.user_data_buffer)) == (
        b.id,
        b.aux_value,
        b.block_runtime_id,
        bytes(b.user_data_buffer),
    )


def resolve(s, name, index, dynamic=None):
    if dynamic is not None:
        raise RuntimeError("dynamic item containers are unsupported")
    if (name == 28 and 0 <= index <= 8) or (name == 29 and 9 <= index <= 35):
        return 0, index
    if (name, index) in ((47, 4), (48, 5)):
        if (
            s.window is None
            or s.container is None
            or s.container.closing
            or s.container.trade is None
            or not s.container.trade.use_new_trade_screen
        ):
            raise RuntimeError("no active new-screen trade window")
        return s.window.window_id, index - 4
    raise RuntimeError("unsupported item container or slot")


def scoped(s, slot):
    s.get_item(slot)
    if slot.area == InventoryArea.player and slot.window is None and 0 <= slot.index <= 35:
        name, index = (28 if slot.index < 9 else 29), slot.index
    elif slot.area == InventoryArea.trade_input and 0 <= slot.index <= 1:
        name, index = 47 + slot.index, 4 + slot.index
    else:
        raise RuntimeError("invalid inventory slot")
    return resolve(s, name, index), name, index


def wire_slot(p, name, index, net_id):
    cls = p.ItemStackRequestSlotInfo if p is bp.v1001 else p.ItemStackRequestCereal.SlotInfoData
    return cls(
        full_container_name=p.FullContainerName(name=p.ContainerEnumName(name), dynamic_id=None),
        slot=index,
        net_id_variant=net_id,
    )


def request(p, request_id, actions):
    cls = p.ItemStackRequestData if p is bp.v1001 else p.ItemStackRequestCereal.RequestData
    return p.ItemStackRequestPacket(
        requests=[
            cls(
                client_request_id=p.ItemStackRequestId(raw_id=request_id),
                actions=actions,
                strings_to_filter=[],
                strings_to_filter_origin=p.TextProcessingEventOrigin.UNKNOWN,
            )
        ]
    )


def transfer(p, count, source, destination, take=False):
    if p is bp.v1001:
        cls = p.ItemStackRequestActionTake if take else p.ItemStackRequestActionPlace
        value = cls(amount=count, src=source, dst=destination)
    else:
        cls = p.ItemStackRequestCereal.TakeActionData if take else p.ItemStackRequestCereal.PlaceActionData
        value = cls(amount=count, source=source, destination=destination)
    return bp.Variant(0 if take else 1, value)


def move(s, source, destination, count):
    s.require_interaction()
    if s.pending is not None:
        raise RuntimeError("an item request is pending")
    if type(count) is not int or not 1 <= count <= 255:
        raise ValueError("item count must be in 1..255")
    src, src_name, src_index = scoped(s, source)
    dst, dst_name, dst_index = scoped(s, destination)
    if src == dst:
        raise RuntimeError("source and destination are the same slot")
    a, b = unwrap(s.get_item(source).item), unwrap(s.get_item(destination).item)
    if not isinstance(a, s.p.SerializedNetworkItemStackDescriptor) or not isinstance(
        b, s.p.SerializedNetworkItemStackDescriptor
    ):
        raise RuntimeError("item slot is unknown or unsupported")  # noqa: TRY004
    if not a.id or a.stack_size < count:
        raise RuntimeError("insufficient source count")
    aid, bid = _stack_id(a), _stack_id(b)
    if aid is None or bid is None:
        raise RuntimeError("item stack ID is not confirmed")
    if b.id and not same_item(a, b):
        raise RuntimeError("destination item is incompatible")
    total = (b.stack_size if b.id else 0) + count
    if total > 65535:
        raise RuntimeError("destination count overflows")
    changes = (
        Change(source, src, a, resized(s.p, a, a.stack_size - count)),
        Change(destination, dst, b, resized(s.p, b if b.id else a, total, bid if b.id else None)),
    )
    actions = [transfer(s.p, count, wire_slot(s.p, src_name, src_index, aid), wire_slot(s.p, dst_name, dst_index, bid))]
    operation = s.submit_item_request(lambda rid: request(s.p, rid, actions), window=s.window)
    s.item_changes = changes
    return operation


def tag(compound, key, kind):
    if compound.type != bp.NbtType.COMPOUND or key not in compound:
        raise RuntimeError(f"missing trade field {key}")
    value = compound[key]
    if value.type != kind:
        raise RuntimeError(f"unsupported trade field {key}")
    return value


def trade_item(value, registry, output):
    if value.type != bp.NbtType.COMPOUND or any(k not in ("Name", "Count", "Damage", "WasPickedUp") for k in value):
        raise RuntimeError("unsupported trade item keys")
    name = tag(value, "Name", bp.NbtType.STRING).value
    count = tag(value, "Count", bp.NbtType.BYTE).value
    damage = tag(value, "Damage", bp.NbtType.SHORT).value
    if not 1 <= count <= 64 or damage not in ((0,) if output else (0, 32767)):
        raise RuntimeError("unsupported trade item Count or Damage")
    if "WasPickedUp" in value and tag(value, "WasPickedUp", bp.NbtType.BYTE).value != 0:
        raise RuntimeError("unsupported trade item WasPickedUp")
    matches = [item for item in registry if item.name == name]
    if len(matches) != 1 or not matches[0].id or matches[0].is_component_based:
        raise RuntimeError("unsupported trade item registry entry")
    item = matches[0]
    if any(entry.id == item.id and entry.name != name for entry in registry):
        raise RuntimeError("ambiguous trade item registry ID")
    return TradeItem(name, item.id, count)


def offers(s, window):
    s.validate_window(window)
    source = s.container.trade
    if source is None:
        return None
    recipes = tag(source.data, "Recipes", bp.NbtType.LIST)
    ids = Counter()
    for row in recipes:
        try:
            ids[tag(row, "netId", bp.NbtType.INT).value & 0xFFFFFFFF] += 1
        except RuntimeError:
            pass
    result = []
    for index, row in enumerate(recipes):
        try:
            net_id = tag(row, "netId", bp.NbtType.INT).value & 0xFFFFFFFF
            if ids[net_id] != 1:
                raise RuntimeError("ambiguous recipe net ID")
            if not source.use_new_trade_screen:
                raise RuntimeError("legacy trade screen does not support Trade2 requests")
            uses = tag(row, "uses", bp.NbtType.INT).value
            maximum = tag(row, "maxUses", bp.NbtType.INT).value
            a = trade_item(tag(row, "buyA", bp.NbtType.COMPOUND), s.registry, False)
            b = trade_item(tag(row, "buyB", bp.NbtType.COMPOUND), s.registry, False) if "buyB" in row else None
            output = trade_item(tag(row, "sell", bp.NbtType.COMPOUND), s.registry, True)
            tier = row["tier"].value if "tier" in row and row["tier"].type == bp.NbtType.INT else None
            result.append(
                TradeOffer(
                    index, TradeOfferRef(window, s.offer_revision, net_id), a, b, output, uses, maximum, tier, True
                )
            )
        except RuntimeError as error:
            result.append(TradeOffer(index, unsupported_reason=str(error)))
    return TradeOffers(window, s.offer_revision, source, tuple(result))


def trade(s, ref, destination):
    s.require_interaction()
    if s.pending is not None:
        raise RuntimeError("an item request is pending")
    s.validate_window(ref.window)
    if ref.revision != s.offer_revision:
        raise RuntimeError("stale trade offer")
    quotes = offers(s, ref.window)
    quote = next((row for row in quotes.offers if row.ref == ref), None) if quotes is not None else None
    if quote is None or not quote.supported:
        raise RuntimeError("unknown or unsupported trade offer")
    if quote.max_uses >= 0 and quote.uses >= quote.max_uses:
        raise RuntimeError("trade is exhausted")
    target, name, index = scoped(s, destination)
    before = s.get_item(destination).item
    if destination.area != InventoryArea.player or before is None or before.id:
        raise RuntimeError("trade destination must be known empty")
    p = s.p
    changes, consumed = [], []
    for offset, ingredient in enumerate((quote.input, quote.second_input)):
        if ingredient is None:
            continue
        slot = s.trade_input(ref.window, offset)
        physical, _, _ = scoped(s, slot)
        item = s.get_item(slot).item
        if (
            item is None
            or item.id != ingredient.network_id
            or item.aux_value
            or item.block_runtime_id
            or bytes(item.user_data_buffer) != PLAIN_DATA
            or item.stack_size < ingredient.count
        ):
            raise RuntimeError("staged trade input is unknown, insufficient or unsupported")
        net_id = _stack_id(item)
        if net_id is None or net_id <= 0:
            raise RuntimeError("staged trade stack ID is not confirmed")
        changes.append(Change(slot, physical, item, resized(p, item, item.stack_size - ingredient.count)))
        source = wire_slot(p, 47 + offset, 4 + offset, net_id)
        action = (
            p.ItemStackRequestActionConsume(amount=ingredient.count, src=source)
            if p is bp.v1001
            else p.ItemStackRequestCereal.ConsumeActionData(amount=ingredient.count, source=source)
        )
        consumed.append(bp.Variant(5, action))
    output = quote.output
    expected = p.SerializedNetworkItemStackDescriptor(
        id=output.network_id,
        stack_size=output.count,
        aux_value=0,
        net_id_variant=None,
        block_runtime_id=0,
        user_data_buffer=PLAIN_DATA,
    )
    changes.append(Change(destination, target, before, expected))
    if p is bp.v1001:
        craft = bp.Variant(
            12, p.ItemStackRequestActionCraftRecipe(recipe_net_id=p.RecipeNetId(raw_id=ref.recipe_net_id), num_crafts=1)
        )
        result = p.SerializedNetworkItemInstanceDescriptor(
            id=output.network_id, stack_size=output.count, aux_value=0, block_runtime_id=0, user_data_buffer=PLAIN_DATA
        )
        results = bp.Variant(
            19, p.ItemStackRequestActionCraftResults_DEPRECATEDASKTYLAING(craft_results=[result], num_crafts=1)
        )
    else:
        c = p.ItemStackRequestCereal
        craft = bp.Variant(
            10, c.CraftRecipeActionData(recipe_net_id=p.RecipeNetId(raw_id=ref.recipe_net_id), num_crafts=1)
        )
        result = c.NetworkItemInstanceDescriptorData(
            item_descriptor=bp.Variant(1, c.ItemNameDescriptorData(full_name=output.identifier, aux_value=0)),
            stack_size=output.count,
            block_runtime_id=0,
            user_data_buffer=PLAIN_DATA,
        )
        results = bp.Variant(17, c.CraftResultsActionData(craft_results=[result], num_crafts=1))

    def packet(rid):
        take = transfer(
            p,
            output.count,
            wire_slot(p, int(p.ContainerEnumName.CREATED_OUTPUT_CONTAINER), 50, rid),
            wire_slot(p, name, index, 0),
            True,
        )
        return request(p, rid, [craft, results, *consumed, take])

    operation = s.submit_item_request(packet, window=ref.window)
    s.item_changes = tuple(changes)
    return operation


def known(s, physical):
    inventory = s.inventories.get(physical[0])
    return None if inventory is None else inventory.slots.get(physical[1])


def reconcile(s, response):
    slots, updates = {}, {}
    changes = {change.physical: change for change in s.item_changes}
    for container in response.containers or ():
        for slot in container.slots:
            if slot.requested_slot != slot.slot:
                raise RuntimeError("ambiguous response slot alias")
            physical = resolve(
                s, int(container.full_container_name.name), slot.slot, container.full_container_name.dynamic_id
            )
            if physical in slots and slots[physical].wire_bytes() != slot.wire_bytes():
                raise RuntimeError("conflicting response slot entries")
            slots[physical] = slot
    for physical, slot in slots.items():
        if not slot.amount:
            updates[physical] = resized(s.p, None, 0)
            continue
        change = changes.get(physical)
        current = known(s, physical)
        authoritative = physical in s.item_authority
        identity = current if (authoritative or change is None) and current is not None and current.id else None
        if identity is None and change is not None:
            identity = change.expected if change.expected.id else change.before
        if identity is None or not identity.id:
            raise RuntimeError("missing response item identity")
        net_id = None if slot.item_stack_net_id is None else slot.item_stack_net_id.raw_id
        if (
            (net_id is None or net_id <= 0)
            and authoritative
            and current is not None
            and current.id
            and current.stack_size == slot.amount
        ):
            net_id = _stack_id(current)
        if net_id is None or net_id <= 0:
            raise RuntimeError("missing confirmed response stack ID")
        if current is not None and current.id and _stack_id(current) == net_id:
            identity = current
        elif identity is current:
            raise RuntimeError("response stack ID conflicts with authoritative item identity")
        updates[physical] = resized(s.p, identity, slot.amount, net_id)
    for physical, change in changes.items():
        if physical in updates:
            continue
        current = known(s, physical)
        expected = change.expected
        if (
            physical not in s.item_authority
            or current is None
            or current.id != expected.id
            or (current.id and current.stack_size != expected.stack_size)
            or _stack_id(current) is None
        ):
            raise RuntimeError("missing response for touched slot")
    for (inventory_id, index), item in updates.items():
        previous = s.inventories.get(inventory_id, InventorySnapshot(MappingProxyType({})))
        values = dict(previous.slots)
        values[index] = item
        s.inventories[inventory_id] = replace(previous, slots=MappingProxyType(values))
