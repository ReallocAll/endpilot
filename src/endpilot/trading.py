"""Observe villager quotes and match staged inputs and exchanges to server verdicts."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from itertools import product
from math import isfinite
from typing import Literal

from bedrock_protocol import nbt

from ._stack_limits_2225 import STACK_LIMITS
from .block_data import lookup_vanilla_block_state, vanilla_block_state
from .containers import _same_slot
from .inventory import _PLAYER_CONTAINERS, _correct_observed_item, _response_item
from .range_transfer import _merge_key


_BlockIdMode = Literal["hashed", "sequential"]


@dataclass(frozen=True, slots=True)
class TradeItem:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class TradeOffer:
    """A server quote whose announced tier can be compared with TradeOffers.trader_tier."""

    recipe_net_id: int | None
    buy_a: TradeItem | None
    buy_b: TradeItem | None
    sell: TradeItem | None
    uses: int | None
    max_uses: int | None
    tier: int | None = None


@dataclass(frozen=True, slots=True)
class TradeOffers:
    window_id: int
    trader_unique_id: int
    display_name: str
    trader_tier: int
    use_new_trade_screen: bool
    offers: tuple[TradeOffer, ...]


@dataclass(frozen=True, slots=True)
class TradeManyRequest:
    """One attempted stage (ingredient 1 or 2) or exchange (ingredient None)."""

    trade: int
    request_id: int
    ingredient: int | None
    inventory_slot: int
    status: Literal["not_sent", "unconfirmed", "rejected", "confirmed"]
    verdict: str | None = None


@dataclass(frozen=True, slots=True)
class TradeManyResult:
    """Independent snapshot of a bounded trade in one server window."""

    recipe_net_id: int
    requested: int
    window_id: int
    trader_unique_id: int
    requests: tuple[TradeManyRequest, ...]
    stop_reason: str

    @property
    def confirmed_count(self) -> int:
        return sum(step.ingredient is None and step.status == "confirmed" for step in self.requests)


@dataclass(frozen=True, slots=True)
class _IngredientTerms:
    damage: int | None
    block_id: int | None
    block_mode: _BlockIdMode | None = None


@dataclass(frozen=True, slots=True)
class _RecipeTerms:
    signature: tuple[tuple[str, str], ...]
    ingredients: tuple[_IngredientTerms, ...]
    plain_output: bool


@dataclass(slots=True)
class _TradeBatch:
    window: object
    recipe_net_id: int
    requested: int
    deadline: float
    terms: tuple[bool, _RecipeTerms] | None
    requests: list[TradeManyRequest] = field(default_factory=list)
    quote_changed: bool = False

    def result(self, reason: str) -> TradeManyResult:
        return TradeManyResult(
            self.recipe_net_id, self.requested, self.window.container_id,
            self.window.entity_unique_id, tuple(self.requests), reason,
        )


@dataclass(slots=True)
class _TradeUseFloor:
    count: int = 0


class _TradeStop(Exception):
    """An observed stop before another request is submitted."""


def _lookup(compound, key: str):
    # Native CompoundTag supports indexing and membership, but not dict.get.
    return compound[key] if key in compound else None


def _number(compound, key: str) -> int | None:
    value = getattr(_lookup(compound, key), "value", None)
    return value if type(value) is int else None


def _item(compound, key: str) -> TradeItem | None:
    value = _lookup(compound, key)
    if not isinstance(value, (nbt.CompoundTag, dict)):
        return None
    name = getattr(_lookup(value, "Name"), "value", None)
    count = _number(value, "Count")
    if not isinstance(name, str) or not name or count is None or not 1 <= count <= 255:
        return None
    return TradeItem(name, count)


def _ingredients(recipe) -> tuple[TradeItem | None, TradeItem | None]:
    buy_a, buy_b = _item(recipe, "buyA"), _item(recipe, "buyB")
    if buy_b is None and ("buyB" in recipe or ("buyCountB" in recipe and _number(recipe, "buyCountB") != 0)):
        # Keep an incomplete two-item quote out of the single-item exchange path.
        return None, None
    return buy_a, buy_b


def _read_offers(packet) -> TradeOffers:
    recipes = _lookup(packet.data, "Recipes")
    offers = tuple(
        TradeOffer(
            _number(recipe, "netId"),
            *_ingredients(recipe),
            _item(recipe, "sell"),
            _number(recipe, "uses"),
            _number(recipe, "maxUses"),
            tier=_number(recipe, "tier"),
        )
        for recipe in (() if recipes is None else recipes)
    )
    return TradeOffers(
        int(packet.container_id), int(packet.entity_unique_id),
        str(packet.display_name), int(packet.trader_tier),
        bool(packet.use_new_trade_screen), offers,
    )


_RECIPE_FIELDS = frozenset({
    "netId", "buyA", "buyB", "sell", "uses", "maxUses", "buyCountA", "buyCountB",
    "demand", "priceMultiplierA", "priceMultiplierB", "rewardExp", "tier", "traderExp",
})
_PLAIN_ITEM_DATA = bytes(10)


def _recipe_terms(recipe, *, economy_price: bool = False, block_id_mode: _BlockIdMode | None = None):
    """Capture a comparable quote and decline ingredients with unmodeled predicates."""
    if set(recipe) - _RECIPE_FIELDS:
        return None
    buy_a, buy_b = _ingredients(recipe)
    if buy_a is None or any(key in recipe and _number(recipe, key) is None for key in ("buyCountA", "buyCountB")):
        return None
    for key in ("priceMultiplierA", "priceMultiplierB"):
        if key in recipe:
            value = getattr(recipe[key], "value", None)
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                return None
    demand_tag = _lookup(recipe, "demand")
    demand = _number(recipe, "demand")
    if "demand" in recipe and (demand is None or demand < 0):
        return None
    base_a = _lookup(recipe, "buyCountA")
    # Native quotes also report a reduced payable count with zero demand.
    adjusted_a = demand is not None and (
        demand > 0 or (isinstance(base_a, nbt.IntTag) and buy_a.count < base_a.value)
    )
    if adjusted_a:
        buy_a_tag = _lookup(recipe, "buyA")
        count_a = _lookup(buy_a_tag, "Count") if isinstance(buy_a_tag, nbt.CompoundTag) else None
        multiplier = _lookup(recipe, "priceMultiplierA")
        if (not economy_price or not isinstance(demand_tag, nbt.IntTag)
                or not isinstance(count_a, nbt.ByteTag) or count_a.value <= 0
                or not isinstance(base_a, nbt.IntTag) or base_a.value <= 0
                or not isinstance(multiplier, nbt.FloatTag) or not isfinite(multiplier.value)
                or multiplier.value < 0):
            return None
    ingredients = []
    for key, count_key, item in (("buyA", "buyCountA", buy_a), ("buyB", "buyCountB", buy_b)):
        if item is None:
            ingredients.append(_IngredientTerms(None, None))
            continue
        ingredient = recipe[key]
        if set(ingredient) - {"Name", "Count", "Damage", "WasPickedUp", "Block"}:
            return None
        # A native economy quote carries the payable amount in the item itself.
        if not (key == "buyA" and adjusted_a) and _number(recipe, count_key) not in (None, item.count):
            return None
        if "WasPickedUp" in ingredient and _number(ingredient, "WasPickedUp") != 0:
            return None
        variant = _number(ingredient, "Damage")
        if "Damage" in ingredient and variant is None:
            return None
        block_id = mode = None
        if "Block" in ingredient:
            if (block_id_mode not in ("hashed", "sequential")
                    or not isinstance(_lookup(ingredient, "Damage"), nbt.ShortTag)
                    or variant is None or variant < 0):
                return None
            block = ingredient["Block"]
            ids = lookup_vanilla_block_state(block)
            if ids is None or block["name"].value != item.name:
                return None
            mode = block_id_mode
            block_id = ids[1] if mode == "hashed" else ids[0]
            if mode == "hashed" and block_id == 0:
                return None
        ingredients.append(_IngredientTerms(None if variant in (None, 32767) else variant, block_id, mode))
    signature = tuple((key, repr(recipe[key])) for key in sorted(recipe)
                      if key not in ("uses", "maxUses", "tier", "traderExp"))
    sell = _lookup(recipe, "sell")
    plain_output = (isinstance(sell, (nbt.CompoundTag, dict))
                    and not (set(sell) - {"Name", "Count", "Damage", "WasPickedUp"})
                    and _number(sell, "Damage") == 0
                    and ("WasPickedUp" not in sell or _number(sell, "WasPickedUp") == 0))
    return _RecipeTerms(signature, tuple(ingredients), plain_output)


def _response_slot(reply, names, index):
    matches = [slot for container in reply.containers or ()
               if container.full_container_name.name.name in names
               for slot in container.slots if int(slot.slot) == index]
    return matches[0] if len(matches) == 1 else None


def _count_at(slot, expected):
    return slot is not None and int(slot.amount) == expected


def _identified_count(slot, expected):
    return (_count_at(slot, expected) and slot.item_stack_net_id is not None
            and int(slot.item_stack_net_id.raw_id) > 0)


def _has_quoted_uses(offer):
    return offer.uses is not None and offer.max_uses is not None and 0 <= offer.uses <= offer.max_uses


class Trading:
    def __init__(self, packets, entities, movement, inventory, containers, item_names=None, *,
                 block_id_mode: Callable[[], _BlockIdMode | None] | None = None):
        self.packets = packets
        self.entities = entities
        self.movement = movement
        self.inventory = inventory
        self.containers = containers
        self.item_names = {} if item_names is None else item_names
        self._block_id_mode = block_id_mode
        self.offers: TradeOffers | None = None
        self._inputs = {}  # number -> (name, amount, confirmed net ID, original item)
        self._quote_window = None
        self._terms = {}
        self._confirmed_uses = {}
        self._unconfirmed = False
        self._requests = {}  # Inventory request ID -> (exact trade window, submitted)
        self._batch: _TradeBatch | None = None
        self._unsubscribe = None if containers is None else packets.subscribe_raw(self._on_packet)

    @property
    def current_offers(self) -> TradeOffers | None:
        """Return the latest server quote for the current trade window instance."""
        if (self._unsubscribe is None or self._quote_window is None
                or self.containers.window is not self._quote_window):
            return None
        return self.offers

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_packet(self, raw):
        if raw.packet_id != self.packets.protocol.binding.UpdateTradePacket.ID:
            return
        packet = self.packets.protocol.decode(raw)
        offers = _read_offers(packet)
        window = self.containers.window
        if (window is None or window.container_type != "TRADE" or window.container_id != offers.window_id
                or window.entity_unique_id != offers.trader_unique_id):
            return
        if self._quote_window is not window:
            self._quote_window = window
            self._inputs.clear()
            self._confirmed_uses.clear()
            self._unconfirmed = False
        else:
            if self.offers.use_new_trade_screen != offers.use_new_trade_screen:
                if self._inputs or self._has_pending(window):
                    self._unconfirmed = True
                self._inputs.clear()
            previous_uses = {quote.recipe_net_id: quote.uses for quote in self.offers.offers if _has_quoted_uses(quote)}
            for quote in offers.offers:
                previous = previous_uses.get(quote.recipe_net_id)
                if previous is not None and _has_quoted_uses(quote) and quote.uses < previous:
                    # Only a decrease across two server observations replaces the local floor.
                    self._confirmed_uses.pop(quote.recipe_net_id, None)
        self.offers = offers
        recipes = _lookup(packet.data, "Recipes")
        economy_price = (getattr(self.packets.protocol, "number", None) == 2225
                         and offers.use_new_trade_screen and getattr(packet, "using_economy_trade", False))
        block_mode = self._block_mode()
        self._terms = {} if recipes is None else {
            _number(recipe, "netId"): _recipe_terms(recipe, economy_price=economy_price, block_id_mode=block_mode)
            for recipe in recipes
        }
        batch = self._batch
        if batch is not None and batch.window is window and self._batch_terms(batch.recipe_net_id) != batch.terms:
            batch.quote_changed = True

    def _trade_window(self):
        offers, window = self.offers, self.containers.window
        if (offers is None or window is None or window.container_type != "TRADE"
                or window.container_id != offers.window_id or window.entity_unique_id != offers.trader_unique_id
                or (self._quote_window is not None and window is not self._quote_window)):
            raise RuntimeError("no matching trade window is open")
        return offers, window

    def _same_layout(self, window, offers):
        return (self.containers.window is window
                and self.offers.use_new_trade_screen == offers.use_new_trade_screen)

    def _offer(self, recipe_net_id):
        return next((offer for offer in self.offers.offers if offer.recipe_net_id == recipe_net_id), None)

    def _batch_terms(self, recipe_net_id):
        terms = self._terms.get(recipe_net_id)
        if terms is not None and sum(offer.recipe_net_id == recipe_net_id for offer in self.offers.offers) == 1:
            return self.offers.use_new_trade_screen, terms
        return None

    def _used(self, offer):
        floor = self._confirmed_uses.get(offer.recipe_net_id)
        return max(offer.uses or 0, 0 if floor is None else floor.count)

    def _require_batch_quote(self, batch):
        if self.containers.window is not batch.window:
            raise _TradeStop("window_changed")
        if batch.quote_changed or not self._supports_quote_block_ids(batch.terms[1]):
            raise _TradeStop("quote_changed")
        offer = self._offer(batch.recipe_net_id)
        if not _has_quoted_uses(offer):
            raise _TradeStop("quote_changed")
        if self._used(offer) >= offer.max_uses:
            raise _TradeStop("quoted_limit")

    def _has_pending(self, window):
        for request_id, (active, submitted) in tuple(self._requests.items()):
            if request_id not in self.inventory._pending_requests:
                del self._requests[request_id]
                if submitted and active is window:
                    self._unconfirmed = True
        return any(active is window for active, _ in self._requests.values())

    def _item_id(self, item):
        return next((id for id, name in self.item_names.items() if name == item.name), None)

    def _can_receive_output(self, stack, item, terms):
        if stack is None:
            return False
        if stack.id == 0:
            return stack.stack_size == 0
        if (not terms.plain_output or getattr(self.packets.protocol, "number", None) != 2225
                or self.item_names.get(int(stack.id)) != item.name):
            return False
        return (int(stack.id) == self._item_id(item) and int(stack.net_id_variant or 0) > 0
                and 0 < stack.stack_size <= STACK_LIMITS.get(item.name, 0) - item.count
                and stack.aux_value == 0 and stack.block_runtime_id == 0
                and bytes(stack.user_data_buffer) == _PLAIN_ITEM_DATA)

    def _require_stable_slot(self, slot, *, request_id=None):
        observed = self.inventory.slots.get(slot)
        own_request = self.inventory._pending_requests.get(request_id)
        if (observed is None or self.inventory._main_transfer_slot_is_unconfirmed(slot)
                or not _same_slot(observed, self.inventory.client_slots().get(slot))
                or any(pending is not own_request for pending in self.inventory._requests_for_endpoints({(None, slot)}))):
            raise _TradeStop("inventory_unobserved")
        return observed

    def _require_output_slot(self, slot, item, terms, *, freed_slot=False):
        observed = self._require_stable_slot(slot)
        if (freed_slot and observed.id != 0) or not self._can_receive_output(observed, item, terms):
            raise _TradeStop("inventory_unobserved")
        return observed

    def _block_mode(self):
        if self._block_id_mode is None or getattr(self.packets.protocol, "number", None) != 2225:
            return None
        return self._block_id_mode()

    def _supports_quote_block_ids(self, terms):
        mode = self._block_mode()
        return all(ingredient.block_mode is None or ingredient.block_mode == mode for ingredient in terms.ingredients)

    def _ingredient_match(self, stack, item, terms) -> bool | None:
        """Return None when a compatible material lacks an identifiable block state."""
        if (stack is None or int(stack.net_id_variant or 0) <= 0 or stack.stack_size <= 0
                or self.item_names.get(int(stack.id)) != item.name
                or (terms.damage is not None and stack.aux_value != terms.damage)):
            return False
        if terms.block_id is None:
            return True
        if stack.aux_value != (terms.damage or 0) or bytes(stack.user_data_buffer) != _PLAIN_ITEM_DATA:
            return False
        hashed = terms.block_mode == "hashed"
        runtime_id = self._block_runtime_id(stack, terms.block_mode)
        if runtime_id == terms.block_id:
            return True
        observed = vanilla_block_state(runtime_id, hashed=hashed)
        if observed is None or (runtime_id == 0 and (hashed or observed.name != item.name)):
            return None
        return False

    @staticmethod
    def _block_runtime_id(stack, mode):
        runtime_id = int(stack.block_runtime_id)
        return runtime_id & 0xFFFFFFFF if mode == "hashed" else runtime_id

    def _input_stack_limit(self, name, stack, terms=None):
        expected = 0 if terms is None or terms.block_id is None else terms.block_id
        mode = None if terms is None else terms.block_mode
        if self._block_runtime_id(stack, mode) != expected:
            return 0
        return self._plain_input_stack_limit(name, stack)

    def _plain_input_stack_limit(self, name, stack):
        if (getattr(self.packets.protocol, "number", None) != 2225
                or stack.aux_value != 0
                or bytes(stack.user_data_buffer) != _PLAIN_ITEM_DATA):
            return 0
        return STACK_LIMITS.get(name, 0)

    def _ingredient_pools(self, item, terms, available):
        matched, unknown = {}, {}
        for index, stack in available.items():
            match = self._ingredient_match(stack, item, terms)
            if match is True:
                matched[index] = stack
            elif match is None:
                unknown[index] = stack

        def pools(stacks, *, assume_state=False):
            singles = sorted((index for index, stack in stacks.items() if stack.stack_size >= item.count),
                             key=lambda index: stacks[index].stack_size)
            groups = {}
            for index, stack in stacks.items():
                key = _merge_key(stack)
                if assume_state:
                    limit = self._plain_input_stack_limit(item.name, stack)
                    key = (key[0], key[1], terms.block_id, key[3])
                else:
                    limit = self._input_stack_limit(item.name, stack, terms)
                if item.count <= limit and stack.stack_size <= limit:
                    groups.setdefault(key, []).append(index)
            split = [tuple(group) for group in groups.values() if len(group) > 1
                     and sum(stacks[index].stack_size for index in group) >= item.count]
            return [(index,) for index in singles] + split

        known = pools(matched)
        # These groups only decide whether missing state can change a zero-send outcome.
        possible = pools(matched | unknown, assume_state=True) if unknown else known
        return known, possible, matched

    @staticmethod
    def _payment_from_freed_slot(costs, pools, counts, destination):
        # For each of the at most two ingredients, the rest of its pool bounds
        # its minimum debit from this slot. The two debits must empty it jointly.
        minimum, maximum = [], []
        for (_, item, _), pool in zip(costs, pools, strict=True):
            capacity_elsewhere = sum(counts[index] for index in pool if index != destination)
            minimum.append(max(0, item.count - capacity_elsewhere))
            maximum.append(item.count if destination in pool else 0)
        if len(pools) == 1:
            minimum.append(0)
            maximum.append(0)
        first = max(minimum[0], counts[destination] - maximum[1])
        second = counts[destination] - first
        if first <= maximum[0] and minimum[1] <= second <= maximum[1]:
            return first, second
        return None

    @staticmethod
    def _allocate_inputs(costs, options, available, *, free_output=False):
        # Try all single-stack assignments before introducing an append step.
        for pools in sorted(product(*options), key=lambda pools: sum(len(pool) > 1 for pool in pools)):
            counts = {index: int(available[index].stack_size) for pool in pools for index in pool}
            for destination in sorted(counts) if free_output else (None,):
                debits = ((0, 0) if destination is None else
                          Trading._payment_from_freed_slot(costs, pools, counts, destination))
                if debits is None:
                    continue
                selected = []
                remaining = counts.copy()
                for position, ((ingredient, item, _), pool) in enumerate(zip(costs, pools, strict=True)):
                    later = pools[position + 1] if position + 1 < len(pools) else ()
                    needed = item.count - debits[position]
                    # Take nonshared stacks first. A freed slot's debits are
                    # already counted against both costs and excluded here.
                    for index in sorted(pool, key=lambda index: index in later):
                        if index == destination:
                            amount = debits[position]
                        else:
                            amount = min(remaining[index], needed)
                            remaining[index] -= amount
                            needed -= amount
                        if amount:
                            selected.append((ingredient, index, amount, available[index]))
                    if needed:
                        break
                else:
                    return sorted(selected, key=lambda step: step[0]), destination
        return None

    def _plan(self, offer, terms):
        slots = self.inventory.slots
        presented = self.inventory.client_slots()
        available = {index: slots[index] for index in range(36)
                     if index in slots and not self.inventory._main_transfer_slot_is_unconfirmed(index)
                     and _same_slot(slots[index], presented.get(index))
                     and not self.inventory._requests_for_endpoints({(None, index)})}
        costs = sorted(((index + 1, item, terms.ingredients[index])
                        for index, item in enumerate((offer.buy_a, offer.buy_b)) if item is not None),
                       key=lambda entry: (entry[2].damage is None, -entry[1].count))
        options, possible_options = [], []
        eligible = {}
        shortage = "insufficient_materials" if len(available) == 36 else "inventory_unobserved"
        enough_each = True
        for _, item, requirement in costs:
            pools, possible, matched = self._ingredient_pools(item, requirement, available)
            enough_each &= sum(stack.stack_size for stack in matched.values()) >= item.count
            options.append(pools)
            possible_options.append(possible)
            eligible.update(matched)
        allocation = self._allocate_inputs(costs, options, available)
        if allocation is None:
            if possible_options != options and self._allocate_inputs(costs, possible_options, available) is not None:
                return "inventory_unobserved"
            # Count each shared source only once across the combined costs.
            if not enough_each or sum(stack.stack_size for stack in eligible.values()) < sum(item.count for _, item, _ in costs):
                return shortage
            return "fragmented_materials"
        sources, _ = allocation
        inputs = {index for _, index, _, _ in sources}
        destination = next((index for index, stack in available.items() if index not in inputs and stack.id
                            and self._can_receive_output(stack, offer.sell, terms)), None)
        if destination is None:
            destination = next((index for index, stack in available.items()
                                if stack.id == 0 and stack.stack_size == 0), None)
        if destination is None:
            allocation = self._allocate_inputs(costs, options, available, free_output=True)
            if allocation is None:
                return "no_output_slot" if len(available) == 36 else "inventory_unobserved"
            return allocation
        return sources, destination

    async def trade_many(self, recipe_net_id: int, count: int, *, timeout: float = 15) -> TradeManyResult:
        """Submit up to count single exchanges in the current trade window.

        Partial stages and exchanges are recorded independently. Errors, including
        timeout and cancellation, carry a frozen trade_progress snapshot. An
        unconfirmed request can still receive a verdict on the shared inventory stream.
        """
        if type(count) is not int or count < 0:
            raise ValueError("trade count must be a nonnegative integer")
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("trade timeout must be finite and nonnegative")
        _, window = self._trade_window()
        offer = self._offer(recipe_net_id)
        if offer is None:
            raise ValueError("no server quote for this recipe net ID")
        terms = self._batch_terms(recipe_net_id)
        deadline = asyncio.get_running_loop().time() + timeout
        batch = _TradeBatch(window, recipe_net_id, count, deadline, terms)
        if not count:
            return batch.result("complete")
        if self._batch is not None or self._has_pending(window):
            return batch.result("trade_busy")
        if self._inputs:
            return batch.result("existing_inputs")
        if self._unconfirmed:
            return batch.result("unconfirmed")
        if (terms is None or not self._supports_quote_block_ids(terms[1]) or offer.buy_a is None or offer.sell is None
                or any(not 1 <= item.count <= 255 for item in (offer.buy_a, offer.buy_b, offer.sell) if item is not None)
                or offer.sell.name not in self.item_names.values()
                or not _has_quoted_uses(offer)):
            return batch.result("unsupported_quote")

        self._batch = batch
        try:
            async with asyncio.timeout_at(deadline):
                for trade in range(1, count + 1):
                    self._require_batch_quote(batch)
                    plan = self._plan(offer, terms[1])
                    if isinstance(plan, str):
                        return batch.result(plan)
                    sources, destination = plan
                    expected = {slot: item for _, slot, _, item in sources}
                    freed_slot = destination in expected
                    for ingredient, source_slot, amount, _ in sources:
                        self._require_batch_quote(batch)
                        source = expected[source_slot]
                        reply = await self._stage_ingredient(
                            source_slot, amount, ingredient=ingredient, timeout=None, batch=batch, trade=trade,
                            output_slot=destination, source_observed=source, output_observed=expected.get(destination),
                        )
                        if batch.requests[-1].status != "confirmed":
                            return batch.result(batch.requests[-1].status)
                        remaining = _response_slot(reply, _PLAYER_CONTAINERS, source_slot)
                        expected[source_slot] = _response_item(self.packets.protocol.binding, source, source, remaining)
                    self._require_batch_quote(batch)
                    await self._exchange(recipe_net_id, destination, timeout=None, batch=batch, trade=trade,
                                         freed_slot=freed_slot)
                    if batch.requests[-1].status != "confirmed":
                        return batch.result(batch.requests[-1].status)
                    if self.containers.window is not window:
                        return batch.result("window_changed")
                    if batch.quote_changed:
                        return batch.result("quote_changed")
            return batch.result("complete")
        except _TradeStop as stop:
            return batch.result(str(stop))
        except (Exception, asyncio.CancelledError) as error:
            reason = ("cancelled" if isinstance(error, asyncio.CancelledError) else
                      "timeout" if isinstance(error, TimeoutError) else "error")
            error.trade_progress = batch.result(reason)
            raise
        finally:
            self._batch = None

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
            if self.containers is None:
                self.offers = offers
            return offers
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _submit(self, request, apply, *, window, timeout, inventory_slot, observed,
                      confirms=None, on_submit=None, on_slot_observed=None, batch=None, trade=0, ingredient=None):
        if self._has_pending(window) or (self._batch is not None and self._batch.window is window
                                         and self._batch is not batch):
            if batch is not None:
                raise _TradeStop("trade_busy")
            raise RuntimeError("another item-stack request owns this trade window")
        endpoints = {(None, inventory_slot)}
        if self.inventory._requests_for_endpoints(endpoints):
            if batch is not None:
                raise _TradeStop("inventory_unobserved")
            raise RuntimeError("the player slot has a pending item-stack request")
        request_id = int(request.client_request_id.raw_id)
        self._requests[request_id] = (window, False)
        step = None
        if batch is not None:
            step = len(batch.requests)
            batch.requests.append(TradeManyRequest(trade, request_id, ingredient, inventory_slot, "not_sent"))
        conflicted = False

        def mark_conflicted():
            nonlocal conflicted
            conflicted = True

        def before_send():
            if batch is not None:
                if self._batch is not batch:
                    raise _TradeStop("cancelled")
                if asyncio.get_running_loop().time() >= batch.deadline:
                    raise TimeoutError("trade timed out before submitting the next request")
                self._require_batch_quote(batch)
                if conflicted or not _same_slot(observed, self._require_stable_slot(inventory_slot, request_id=request_id)):
                    raise _TradeStop("inventory_unobserved")
            elif self.containers.window is not window:
                raise RuntimeError("trade window changed before submitting the request")
            if on_submit is not None:
                on_submit()
            if batch is not None:
                batch.requests[step] = replace(batch.requests[step], status="unconfirmed")
            self._requests[request_id] = (window, True)

        def receive(reply):
            try:
                apply(reply)
                if confirms is None:
                    return
                verdict = reply.result.name
                if verdict != "SUCCESS":
                    status = "rejected"
                elif not conflicted and confirms(reply):
                    status = "confirmed"
                else:
                    status = "unconfirmed"
                    if self.containers.window is window:
                        self._unconfirmed = True
                if batch is not None:
                    batch.requests[step] = replace(batch.requests[step], status=status, verdict=verdict)
            except Exception:
                if self.containers.window is window:
                    self._unconfirmed = True
                raise
            finally:
                self._requests.pop(request_id, None)

        try:
            return await self.inventory._request_item_stack(
                request, receive, timeout=timeout, before_send=before_send if batch is None else None,
                _at_write=before_send if batch is not None else None,
                _on_unconfirmed=mark_conflicted if batch is not None else None,
                _on_slot_observed=on_slot_observed,
            )
        except (Exception, asyncio.CancelledError):
            if request_id not in self.inventory._pending_requests:
                _, submitted = self._requests.pop(request_id, (None, False))
                if submitted and self.containers.window is window:
                    self._unconfirmed = True
            raise

    def _apply_player_correction(self, index, original, previous, fallback, slot):
        current = self.inventory.slots.get(index)
        b = self.packets.protocol.binding
        if _same_slot(original, current):
            if not int(slot.amount) or int(previous.id) or int(fallback.id):
                self.inventory._set_slot(index, _response_item(b, previous, fallback, slot))
        elif int(slot.amount) > 0:
            # A replacement can only be corrected by a reply naming its stack ID.
            updated = _correct_observed_item(b, current, slot)
            if updated is not None:
                self.inventory._set_slot(index, updated)

    def _apply_input_correction(self, ingredient, original, slot, success):
        if slot is None and not success:
            return
        if (original is not None and slot is not None and int(slot.amount) > 0
                and slot.item_stack_net_id is not None and int(slot.item_stack_net_id.raw_id) > 0):
            name, _, _, item = original
            self._inputs[ingredient] = (name, int(slot.amount), int(slot.item_stack_net_id.raw_id), item)
        else:
            self._inputs.pop(ingredient, None)
            if not _count_at(slot, 0):
                self._unconfirmed = True

    async def stage_ingredient(
        self, source_slot: int, amount: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Move an observed player stack into the open trade input; return BDS's verdict.

        This stages an ingredient only; it does not select or complete a trade.
        """
        return await self._stage_ingredient(source_slot, amount, ingredient=ingredient, timeout=timeout)

    async def _stage_ingredient(self, source_slot, amount, *, ingredient, timeout, batch=None, trade=0,
                                output_slot=None, source_observed=None, output_observed=None):
        offers, window = self._trade_window()
        if not 0 <= source_slot <= 255:
            raise ValueError("source slot must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        if ingredient not in (1, 2):
            raise ValueError("ingredient must be 1 or 2")
        source = self.inventory.slots.get(source_slot)
        requirement = None
        if batch is not None:
            offer = self._offer(batch.recipe_net_id)
            item = (offer.buy_a, offer.buy_b)[ingredient - 1]
            requirement = batch.terms[1].ingredients[ingredient - 1]
            if (not _same_slot(source, source_observed) or not self._ingredient_match(source, item, requirement)
                    or source.stack_size < amount):
                raise _TradeStop("inventory_unobserved")
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")
        previous = self._inputs.get(ingredient)
        total = amount if previous is None else previous[1] + amount
        destination_id = 0 if previous is None else previous[2]
        source_name = self.item_names.get(int(source.id))
        if previous is not None and (source_name != previous[0] or _merge_key(source) != _merge_key(previous[3])
                                     or total > self._input_stack_limit(source_name, source, requirement)):
            if batch is not None:
                raise _TradeStop("inventory_unobserved")
            raise ValueError("trade input and source are not confirmed compatible stacks")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        destination_name = (
            f"TRADE2_INGREDIENT_{ingredient}_CONTAINER"
            if offers.use_new_trade_screen else f"TRADE_INGREDIENT_{ingredient}_CONTAINER"
        )
        physical_slot = ingredient + 3 if offers.use_new_trade_screen else 0

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
                destination=slot_info(destination_name, physical_slot, destination_id),
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def apply(response):
            # The reply carries authoritative counts/net IDs, including corrections
            # on failure. Do not infer the outcome from sending the request.
            player = _response_slot(response, _PLAYER_CONTAINERS, source_slot)
            if player is not None:
                self._apply_player_correction(source_slot, source, source, source, player)
            if self._same_layout(window, offers):
                slot = _response_slot(response, (destination_name,), physical_slot)
                success = response.result.name == "SUCCESS"
                if success and not _identified_count(slot, total):
                    self._inputs.pop(ingredient, None)
                    self._unconfirmed = True
                    return
                if (not success and slot is not None and int(slot.amount) > 0
                        and (previous is None or not _identified_count(slot, previous[1])
                             or int(slot.item_stack_net_id.raw_id) != previous[2])):
                    self._inputs.pop(ingredient, None)
                    self._unconfirmed = True
                    return
                original = previous
                if success and original is None and source_name is not None:
                    original = (source_name, amount, source.net_id_variant, source)
                self._apply_input_correction(ingredient, original, slot, success)

        def confirms(reply):
            remainder = int(source.stack_size) - amount
            player = _response_slot(reply, _PLAYER_CONTAINERS, source_slot)
            source_confirmed = _identified_count(player, remainder) if remainder else _count_at(player, 0)
            return (source_confirmed
                    and _identified_count(_response_slot(reply, (destination_name,), physical_slot), total))

        def on_submit():
            if self._inputs.get(ingredient) is not previous or self._unconfirmed:
                if batch is not None:
                    raise _TradeStop("unconfirmed")
                raise RuntimeError("trade input needs a fresh confirmed observation")
            if batch is not None:
                if not self._ingredient_match(self.inventory.slots.get(source_slot), item, requirement):
                    raise _TradeStop("inventory_unobserved")
                if output_observed is None:
                    self._require_output_slot(output_slot, offer.sell, batch.terms[1])
                elif not _same_slot(output_observed, self._require_stable_slot(output_slot, request_id=request_id)):
                    raise _TradeStop("inventory_unobserved")
            elif previous is not None and (self.inventory._main_transfer_slot_is_unconfirmed(source_slot)
                                           or int(source.net_id_variant or 0) <= 0 or source.stack_size < amount
                                           or not _same_slot(source, self.inventory.client_slots().get(source_slot))):
                raise RuntimeError("trade source needs a fresh confirmed observation")

        return await self._submit(request, apply, window=window, timeout=timeout, inventory_slot=source_slot,
                                  observed=source, confirms=confirms, on_submit=on_submit,
                                  batch=batch, trade=trade, ingredient=ingredient)

    async def unstage_ingredient(
        self, destination_slot: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Return a server-confirmed trade input to inventory before closing the window.

        BDS 1.26.52 does not refund staged inputs merely because the trade window
        closes. Do not report success until its ItemStackResponse accepts the move.
        """
        offers, window = self._trade_window()
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        if ingredient not in (1, 2):
            raise ValueError("ingredient must be 1 or 2")
        staged = self._inputs.get(ingredient)
        if staged is None:
            raise RuntimeError("trade ingredient lacks a confirmed staged stack")
        _, amount, net_id, original = staged
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        container_name = (f"TRADE2_INGREDIENT_{ingredient}_CONTAINER"
                          if offers.use_new_trade_screen else
                          f"TRADE_INGREDIENT_{ingredient}_CONTAINER")
        physical_slot = ingredient + 3 if offers.use_new_trade_screen else 0
        destination_observed = self.inventory.slots.get(destination_slot)
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

        def apply(reply):
            if self._same_layout(window, offers):
                slot = _response_slot(reply, (container_name,), physical_slot)
                self._apply_input_correction(ingredient, staged, slot, reply.result.name == "SUCCESS")
            player = _response_slot(reply, _PLAYER_CONTAINERS, destination_slot)
            if player is not None:
                self._apply_player_correction(destination_slot, destination_observed, destination, original, player)

        return await self._submit(request, apply, window=window, timeout=timeout,
                                  inventory_slot=destination_slot, observed=destination_observed)

    async def exchange(self, recipe_net_id: int, destination_slot: int, *, timeout: float = 15):
        """Submit a quoted exchange and return the matching BDS ItemStackResponse.

        Only server-reported response slots update the observed input and output state.
        """
        return await self._exchange(recipe_net_id, destination_slot, timeout=timeout)

    async def _exchange(self, recipe_net_id, destination_slot, *, timeout, batch=None, trade=0, freed_slot=False):
        offers, window = self._trade_window()
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        offer = self._offer(recipe_net_id)
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
            if (staged is None or staged[0] != item.name or (batch is not None and staged[1] != item.count)
                    or staged[1] < item.count or staged[2] <= 0):
                raise RuntimeError(f"trade ingredient {number} lacks a confirmed staged stack")
            name = (f"TRADE2_INGREDIENT_{number}_CONTAINER"
                    if offers.use_new_trade_screen else f"TRADE_INGREDIENT_{number}_CONTAINER")
            slot = number + 3 if offers.use_new_trade_screen else 0
            consumes.append(cereal.ConsumeActionData(
                amount=item.count, source=slot_info(name, slot, staged[2]),
            ))
            input_slots[(name, slot)] = (number, staged, item.count)

        terms = self._terms.get(recipe_net_id) if batch is None else batch.terms[1]
        destination_observed = (self.inventory.slots.get(destination_slot) if batch is None else
                                self._require_output_slot(destination_slot, offer.sell, terms, freed_slot=freed_slot))
        destination = self.inventory.item_or_empty(destination_slot)
        observe_destination = batch is not None and (destination.id != 0 or freed_slot)
        output_observation = None
        output_conflicted = False
        use_floor = None
        used_before = 0
        output_id = self._item_id(offer.sell)
        request_id = self.inventory._allocate_request_id()
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

        def on_submit():
            nonlocal use_floor, used_before
            if batch is not None:
                for number, staged, _ in input_slots.values():
                    item = (offer.buy_a, offer.buy_b)[number - 1]
                    requirement = batch.terms[1].ingredients[number - 1]
                    if self._inputs.get(number) is not staged or not self._ingredient_match(staged[3], item, requirement):
                        raise _TradeStop("inventory_unobserved")
            use_floor = self._confirmed_uses.setdefault(recipe_net_id, _TradeUseFloor())
            used_before = self._used(self._offer(recipe_net_id) or offer)

        def observe_output(_endpoint, current):
            nonlocal output_observation, output_conflicted
            if output_observation is None and _same_slot(destination_observed, current):
                return
            if current is None or (output_observation is not None and not _same_slot(output_observation, current)):
                output_conflicted = True
            else:
                output_observation = current

        def apply(reply):
            nonlocal output_conflicted
            success = reply.result.name == "SUCCESS"
            if self._same_layout(window, offers):
                for (name, index), (number, original, _) in input_slots.items():
                    slot = _response_slot(reply, (name,), index)
                    self._apply_input_correction(number, original, slot, success)
            player = _response_slot(reply, _PLAYER_CONTAINERS, destination_slot)
            if player is not None:
                output = destination
                if success and output_id is not None and int(destination.id) != output_id:
                    output = b.SerializedNetworkItemStackDescriptor(
                        id=output_id, stack_size=offer.sell.count, aux_value=0, net_id_variant=None,
                        block_runtime_id=0,
                        user_data_buffer=_PLAIN_ITEM_DATA if terms is not None and terms.plain_output else b"",
                    )
                if observe_destination:
                    corrected = _response_item(b, output, output, player)
                    current = self.inventory.slots.get(destination_slot)
                    if (output_conflicted or self.inventory._main_transfer_slot_is_unconfirmed(destination_slot)
                            or (output_observation is not None and not _same_slot(output_observation, corrected))
                            or not (_same_slot(destination_observed, current) or _same_slot(corrected, current))):
                        # A receipt carries a count and stack ID, so keep a conflicting independent description.
                        output_conflicted = True
                        return
                    if _same_slot(corrected, current):
                        return
                self._apply_player_correction(destination_slot, destination_observed, output, output, player)

        def confirms(reply):
            if output_conflicted:
                return False
            for (name, index), (_, staged, amount) in input_slots.items():
                slot = _response_slot(reply, (name,), index)
                remainder = staged[1] - amount
                if not (_identified_count(slot, remainder) if remainder else _count_at(slot, 0)):
                    return False
            product = _response_slot(reply, _PLAYER_CONTAINERS, destination_slot)
            if _identified_count(product, int(destination.stack_size) + offer.sell.count):
                if use_floor is not None:
                    # A later quote reset or window replacement detached this floor.
                    use_floor.count = used_before + 1
                return True
            return False

        return await self._submit(request, apply, window=window, timeout=timeout, inventory_slot=destination_slot,
                                  observed=destination_observed, confirms=confirms, on_submit=on_submit,
                                  on_slot_observed=observe_output if observe_destination else None,
                                  batch=batch, trade=trade)
