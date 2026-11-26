"""Submit fishing actions and observe the server's hook and pickup evidence."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import dist
from typing import Literal

from .collection import _drop_identity
from .entities import Entities, EntityState
from .inventory_observation import _InventoryObservation
from .item_use import ItemUse


_HOOK = "minecraft:fishing_hook"
_ROD = "minecraft:fishing_rod"
# In the stock server trace the item was born at the hook's X/Z, with a
# small Y offset. Use the currently observed hook rather than the player's feet.
_DROP_RADIUS = 0.5


@dataclass(frozen=True, slots=True)
class FishingResult:
    """Observed fishing activity during one reel, including a verified personal receipt."""

    hook: EntityState
    status: Literal["received", "empty_reel", "drop_unobserved", "pickup_unconfirmed", "inventory_unconfirmed",
                    "taken_by_other", "ambiguous", "reel_unconfirmed", "interrupted"]
    bite_observed: bool
    retracted: bool
    drops: tuple[EntityState, ...]
    pickups: tuple[tuple[int, int], ...]  # (item runtime ID, collector runtime ID)
    inventory_delta: int | None = None
    received_count: int | None = None


def _hook_key(hook: EntityState | None) -> tuple[int, int] | None:
    return None if hook is None else (hook.entity_id, hook.runtime_id)


class _ReelObservation:
    def __init__(self, fishing: "Fishing", hook: EntityState):
        self.fishing = fishing
        self.hook = hook
        self.bite_observed = fishing._last_bite == _hook_key(hook)
        self.inventory = _InventoryObservation(fishing.item_use.inventory, require_all_slots=True)
        self.known = {drop.runtime_id: drop for drop in fishing.entities.drops()}
        self.seen = {_drop_identity(drop) for drop in self.known.values()}
        self.drops = {}
        self.takes = {}
        self.other_hooks = {actor.entity_id for actor in fishing.entities.snapshot()
                            if actor.actor_type == _HOOK and actor.entity_id != hook.entity_id}
        self.competing_reel = False
        self.retracted = False
        self.interrupted = False

    def observe(self, raw) -> bool:
        protocol = self.fishing.packets.protocol
        b = protocol.binding
        if self.fishing.item_use._interrupts(raw):
            self.interrupted = True
            return True
        if raw.packet_id == b.RemoveActorPacket.ID:
            actor = protocol.decode(raw).entity_id
            if actor == self.hook.entity_id:
                self.retracted = True
            elif actor in self.other_hooks:
                self.other_hooks.discard(actor)
                self.competing_reel = True
        elif raw.packet_id == b.AddActorPacket.ID:
            packet = protocol.decode(raw)
            # A runtime ID reused by a new actor cannot still name an older drop.
            self.known.pop(int(packet.runtime_id), None)
            if packet.actor_type == _HOOK and packet.entity_id != self.hook.entity_id:
                self.other_hooks.add(int(packet.entity_id))
        elif raw.packet_id == b.AddItemActorPacket.ID:
            packet = protocol.decode(raw)
            drop = self.fishing.entities.get(int(packet.runtime_id))
            if drop is not None:
                self.known[drop.runtime_id] = drop
                identity = _drop_identity(drop)
                if (identity not in self.seen and drop.is_from_fishing and drop.item_network_id
                    and drop.item_count and dist(drop.position, self.hook.position) <= _DROP_RADIUS):
                    self.drops[identity] = drop
                self.seen.add(identity)
        elif raw.packet_id == b.TakeItemActorPacket.ID:
            packet = protocol.decode(raw)
            item_id, player_id = int(packet.item_id), int(packet.actor_id)
            drop = self.known.get(item_id)
            identity = None if drop is None else _drop_identity(drop)
            self.takes[(identity, item_id, player_id)] = drop
        elif raw.packet_id in (b.InventorySlotPacket.ID, b.InventoryContentPacket.ID,
                               b.InventoryTransactionPacket.ID, b.ItemStackResponsePacket.ID):
            self.inventory.observe(raw)
        return False

    def result(self) -> FishingResult:
        drops = tuple(self.drops.values())
        delta = received = None
        if self.interrupted:
            status = "interrupted"
        elif not self.retracted:
            status = "reel_unconfirmed"
        elif len(drops) > 1 or (drops and self.competing_reel):
            status = "ambiguous"
        elif not drops:
            status = "drop_unobserved" if self.bite_observed else "empty_reel"
        else:
            drop, = drops
            identity = _drop_identity(drop)
            actor = self.fishing.item_use.runtime_id
            collectors = {player for (key, _, player) in self.takes if key == identity}
            delta = self.inventory.delta(drop.item_network_id)
            if len(collectors) > 1 or any(
                key != identity and player == actor and (item is None or item.item_network_id == drop.item_network_id)
                for (key, _, player), item in self.takes.items()
            ):
                status = "ambiguous"
            elif not collectors:
                status = "pickup_unconfirmed"
            elif actor not in collectors:
                status = "taken_by_other"
            elif delta == drop.item_count:
                status, received = "received", delta
            else:
                status = "inventory_unconfirmed"
        return FishingResult(
            self.hook, status, self.bite_observed, self.retracted, drops,
            tuple((item, player) for key, item, player in self.takes if key in self.drops), delta, received,
        )


class Fishing:
    def __init__(self, entities: Entities, item_use: ItemUse, item_names: dict[int, str], player_entity_id: int):
        self.entities = entities
        self.item_use = item_use
        self.packets = entities.packets
        self._item_names = item_names
        self._player_entity_id = player_entity_id
        self._last_bite = None
        self._reel_removals: dict[int, int] = {}
        self._ended = asyncio.Event()
        self._action = asyncio.Lock()
        self._unsubscribe = self.packets.subscribe_raw(self._observe_session)
        subscribe_reader_end = getattr(self.packets, "subscribe_reader_end", None)
        self._unsubscribe_end = None if subscribe_reader_end is None else subscribe_reader_end(self._end_session)

    def close(self) -> None:
        self._unsubscribe()
        if self._unsubscribe_end is not None:
            self._unsubscribe_end()
        self._end_session()

    def _end_session(self, _cause=None) -> None:
        self._ended.set()
        self._last_bite = None
        self._reel_removals.clear()

    def _session_ended(self) -> bool:
        reader = getattr(self.packets, "_reader_task", None)
        return self._ended.is_set() or (reader is not None and reader.done())

    def _require_live(self) -> None:
        if self._session_ended():
            raise ConnectionError("fishing session ended")

    async def _while_live(self, operation):
        task = asyncio.ensure_future(operation)
        ended = asyncio.create_task(self._ended.wait())
        try:
            self._require_live()
            await asyncio.wait((task, ended), return_when=asyncio.FIRST_COMPLETED)
        finally:
            task.cancel()
            ended.cancel()
            await asyncio.gather(task, ended, return_exceptions=True)
        self._require_live()
        return task.result()

    @property
    def hook(self) -> EntityState | None:
        if self._session_ended():
            return None
        hooks = [actor for actor in self.entities.snapshot() if self._owns(actor)]
        if len(hooks) > 1:
            raise RuntimeError("multiple owned fishing hooks are observed")
        return hooks[0] if hooks else None

    def _owns(self, actor: EntityState | None) -> bool:
        return (actor is not None and actor.actor_type == _HOOK and bool(actor.entity_id) and actor.owner_entity_id is not None
                and actor.owner_entity_id == self._player_entity_id)

    def _observe_session(self, raw) -> None:
        if self._ended.is_set():
            return
        protocol = self.packets.protocol
        b = protocol.binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self._end_session()
        elif self.item_use._interrupts(raw):
            self._last_bite = None
        elif raw.packet_id == b.ActorEventPacket.ID:
            event = protocol.decode(raw)
            if int(event.event_id) == int(b.ActorEvent.FISHHOOK_HOOKTIME):
                hook = self.entities.get(int(event.runtime_id))
                if self._owns(hook):
                    self._last_bite = _hook_key(hook)
        elif raw.packet_id == b.RemoveActorPacket.ID and (self._last_bite or self._reel_removals):
            actor_id = protocol.decode(raw).entity_id
            if self._last_bite is not None and actor_id == self._last_bite[0]:
                self._last_bite = None
            if actor_id in self._reel_removals:
                self._reel_removals[actor_id] += 1

    @property
    def _hook_packets(self) -> tuple[int, ...]:
        b = self.packets.protocol.binding
        return (b.AddActorPacket.ID, b.RemoveActorPacket.ID, b.ActorEventPacket.ID,
                b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID, b.RespawnPacket.ID)

    @asynccontextmanager
    async def _observing(self, packet_ids, matches):
        started = False

        def begin():
            nonlocal started
            started = True

        waiting = asyncio.create_task(self.packets.wait_for_packet(
            packet_ids, match=lambda raw: started and matches(raw), timeout=None,
        ))
        try:
            await asyncio.sleep(0)
            yield waiting, begin
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def _submit(self, hook: EntityState | None, begin) -> None:
        def before_send():
            self._require_live()
            if _hook_key(self.hook) != _hook_key(hook):
                raise RuntimeError("the fishing hook changed before submission")
            if hook is not None and hook.entity_id in self._reel_removals:
                raise RuntimeError("a reel was already submitted for this hook")
            inventory = self.item_use.inventory
            item = inventory.item_or_empty(inventory.selected_slot)
            if not item.stack_size or self._item_names.get(int(item.id)) != _ROD:
                raise ValueError("select an observed fishing rod before fishing")
            begin()
            if hook is not None:
                # At transport entry delivery can become uncertain. Another
                # use after this point could cast a new hook.
                self._reel_removals[hook.entity_id] = 0

        await self.item_use.submit(before_send=before_send)

    async def cast(self, *, timeout: float) -> EntityState:
        async with self._action, asyncio.timeout(timeout):
            self._require_live()
            if self.hook is not None:
                raise RuntimeError("a fishing hook is already observed")
            self._last_bite = None
            hook = None

            def matches(raw):
                nonlocal hook
                if self.item_use._interrupts(raw):
                    raise ConnectionError("fishing cast interrupted")
                hook = self.hook
                return hook is not None

            async with self._observing(self._hook_packets, matches) as (waiting, begin):
                await self._while_live(self._submit(None, begin))
                await self._while_live(waiting)
            self._require_live()
            return hook

    async def wait_for_bite(self, *, timeout: float, hook: EntityState | None = None) -> EntityState:
        self._require_live()
        hook = self.hook if hook is None else hook
        key = _hook_key(hook)
        if hook is None or _hook_key(self.hook) != key:
            raise RuntimeError("no matching owned fishing hook")
        if self._last_bite == key:
            return self.hook

        def matches(raw):
            if self.item_use._interrupts(raw):
                raise ConnectionError("fishing wait interrupted")
            if _hook_key(self.hook) != key:
                raise RuntimeError("the fishing hook was removed")
            return self._last_bite == key

        await self._while_live(self.packets.wait_for_packet(self._hook_packets, timeout=timeout, match=matches))
        return hook

    async def reel(self, *, timeout: float, hook: EntityState | None = None) -> FishingResult:
        async with self._action:
            self._require_live()
            current = self.hook
            if current is None or (hook is not None and _hook_key(current) != _hook_key(hook)):
                raise RuntimeError("no matching owned fishing hook")
            if current.entity_id in self._reel_removals:
                bite = self._last_bite == _hook_key(current)
                retracted = False
                try:
                    async with asyncio.timeout(timeout):
                        await self._retract(current)
                        retracted = True
                except TimeoutError:
                    self._require_live()
                return FishingResult(current, "reel_unconfirmed", bite, retracted, (), ())
            observation = None
            b = self.packets.protocol.binding
            packets = self._hook_packets + (
                b.AddItemActorPacket.ID, b.TakeItemActorPacket.ID,
                b.InventorySlotPacket.ID, b.InventoryContentPacket.ID, b.InventoryTransactionPacket.ID,
                b.ItemStackResponsePacket.ID,
            )
            async with self._observing(packets, lambda raw: observation.observe(raw)) as (waiting, begin):
                def bind():
                    nonlocal observation
                    observation = _ReelObservation(self, self.hook)
                    begin()

                await self._while_live(self._submit(current, bind))
                try:
                    async with asyncio.timeout(timeout):
                        await self._while_live(waiting)
                except TimeoutError:
                    pass  # The window ended. Only observed packets establish the result.
            self._require_live()
            assert observation is not None
            return observation.result()

    async def cancel(self, *, timeout: float, expected: EntityState | None = None) -> bool:
        async with self._action, asyncio.timeout(timeout):
            self._require_live()
            hook = self.hook
            if hook is None or (expected is not None and _hook_key(hook) != _hook_key(expected)):
                return False
            await self._retract(hook)
            return True

    async def _retract(self, hook: EntityState) -> None:
        protocol = self.packets.protocol
        b = protocol.binding
        removals = self._reel_removals.get(hook.entity_id)

        def removed(raw):
            if self.item_use._interrupts(raw):
                raise ConnectionError("fishing cancellation interrupted")
            return raw.packet_id == b.RemoveActorPacket.ID and protocol.decode(raw).entity_id == hook.entity_id

        async with self._observing(self._hook_packets, removed) as (waiting, begin):
            self._require_live()
            if removals is not None:
                # The exact removal may arrive while the waiter arms.
                if self._reel_removals[hook.entity_id] != removals:
                    return
                begin()
            else:
                await self._while_live(self._submit(hook, begin))
            await self._while_live(waiting)
        self._require_live()

    async def fish(self, *, bite_timeout: float, cast_timeout: float, reel_timeout: float) -> FishingResult:
        hook = await self.cast(timeout=cast_timeout)
        try:
            await self.wait_for_bite(timeout=bite_timeout, hook=hook)
        except (asyncio.CancelledError, TimeoutError):
            try:
                await self.cancel(timeout=cast_timeout, expected=hook)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError):
                pass
            raise
        return await self.reel(timeout=reel_timeout, hook=hook)
