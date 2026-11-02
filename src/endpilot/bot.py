from __future__ import annotations

import asyncio
from dataclasses import fields, replace

from ._projection import ReadonlyMap
from .events import validate_mask
from .observations import ItemRegistrySnapshot, Snapshot, Statistics, TradeOffers
from .operations import ItemOperation
from .types import (
    BotOptions,
    BotToken,
    EntityRef,
    InventorySlot,
    ItemRequestKey,
    ObservedEntity,
    TradeOfferRef,
    Vec3,
    WindowRef,
    _CapturedOptions,
)


def _future(loop):
    future = loop.create_future()
    future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    return future


class Bot:
    def __init__(self, runtime, options: BotOptions):
        self._runtime = runtime
        self._options = replace(options)
        self._options._validate()
        self._token = BotToken()
        self._interests = 0
        self._generation = 1
        self._ready = _future(runtime._loop)
        self._completion = _future(runtime._loop)
        self._intent = "connect"
        self._reason = ""
        self._transition = True
        self._wake = asyncio.Event()
        self._session = None
        self._lifetime = runtime._loop.create_task(self._run(), name="endpilot-lifetime")

    def _guard(self, *, mutable=False):
        self._runtime._guard(deliver=mutable)

    def _request(self, intent, reason=""):
        if intent == "disconnect" and self._runtime._stopping:
            return asyncio.shield(self._completion)
        if self._transition and self._intent == intent:
            return asyncio.shield(self._completion)
        if not self._transition and intent == "disconnect" and (self._session is None or self._session.finished):
            done = self._runtime._loop.create_future()
            done.set_result(None)
            return done
        if not self._completion.done():
            self._completion.cancel()
        self._completion = _future(self._runtime._loop)
        self._intent = intent
        self._reason = reason
        self._transition = True
        if not self._ready.done():
            self._ready.set_exception(RuntimeError("session superseded or closed"))
        if intent == "connect":
            if self._generation >= (1 << 64) - 1:
                raise RuntimeError("session IDs exhausted")
            self._generation += 1
            self._ready = _future(self._runtime._loop)
        if self._session is not None:
            self._session.invalidate(reason or "session closed")
        self._wake.set()
        return asyncio.shield(self._completion)

    async def _join_session_runner(self, runner):
        if (
            not runner.done()
            and not runner.cancelling()
            and self._session.started
            and not self._session.cleaning
        ):
            runner.cancel()
        await runner

    async def _run(self):
        from .session import Session

        runner = None
        try:
            while True:
                if self._transition:
                    self._wake.clear()
                    if runner is not None:
                        await self._join_session_runner(runner)
                        runner = None
                    intent = self._intent
                    if intent == "connect" and not self._runtime._stopping:
                        self._session = Session(self, self._generation, self._ready)
                        runner = asyncio.create_task(self._session.run(), name="bedrock-connection")
                    self._transition = False
                    if not self._completion.done():
                        self._completion.set_result(None)
                    if intent == "stop" or self._runtime._stopping:
                        return
                await self._wake.wait()
                self._wake.clear()
        except BaseException as exc:
            self._runtime._background_failure(exc)
            if not self._completion.done():
                self._completion.set_exception(exc)
            if runner is not None:
                await self._join_session_runner(runner)

    def disconnect(self, reason: str = ""):
        self._guard(mutable=True)
        return self._request("disconnect", reason)

    def reconnect(self):
        self._guard(mutable=True)
        if self._runtime._stopping:
            raise RuntimeError("Runtime is stopping")
        return self._request("connect", "reconnecting")

    def wait_ready(self, timeout: float | None = None):
        self._guard(mutable=True)
        if self._intent != "connect" or self._runtime._stopping:
            raise RuntimeError("session is closed or invalidated")
        ready = self._ready
        generation = self._generation

        async def wait():
            self._guard()
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.wait((ready,))
                    ready.result()
                self._guard(mutable=True)
                if (
                    generation != self._generation
                    or ready is not self._ready
                    or self._session is None
                    or self._session.id != generation
                    or not self._session.usable()
                ):
                    raise RuntimeError("session is closed or invalidated")
            except Exception:
                self._runtime._deliver_failure()
                raise

        return wait()

    def _current(self, *, mutable=False):
        self._guard(mutable=mutable)
        if self._session is None:
            raise RuntimeError("session has not started")
        return self._session

    def get_status(self):
        self._guard()
        if self._session is None:
            from .types import BotState, BotStatus

            state = BotState.scheduled if self._intent == "connect" and not self._runtime._stopping else BotState.closed
            return BotStatus(session_id=self._generation, state=state)
        return self._session.status

    def get_event_interests(self):
        self._guard()
        return self._interests

    def set_event_interests(self, mask):
        self._guard(mutable=True)
        self._interests = validate_mask(mask)

    def capture_options(self):
        self._guard()
        captured = object.__new__(_CapturedOptions)
        for field in fields(self._options):
            object.__setattr__(captured, field.name, getattr(self._options, field.name))
        return captured

    def capture_statistics(self):
        self._guard()
        session = self._session
        if session is None:
            return Statistics()
        return Statistics(
            **session.stats,
            packet_counts_in=session.packet_counts_in,
            packet_counts_out=session.packet_counts_out,
            play_packet_counts_in=session.play_packet_counts_in,
            play_packet_counts_out=session.play_packet_counts_out,
        )

    def capture_snapshot(self):
        s = self._current()
        return Snapshot(
            **{f.name: getattr(s.status, f.name) for f in fields(s.status)},
            actors=self.capture_actors(),
            inventories=s.inventories,
            item_registry=s.registry,
            container=s.container,
        )

    def capture_actor(self, runtime_id):
        return self._current().actors.get(runtime_id)

    def capture_actors(self):
        return ReadonlyMap(self._current().actors)

    def capture_inventory(self, inventory_id):
        return self._current().inventories.get(inventory_id)

    def capture_container(self):
        return self._current().container

    def capture_item_registry(self):
        s = self._current()
        return ItemRegistrySnapshot(s.registry_observed, s.registry)

    def get_observed_entities(self):
        s = self._current()
        return [
            ObservedEntity(EntityRef(self._token, s.id, a.runtime_id, a.unique_id), a.type, a.position)
            for a in s.actors.values()
        ]

    def send_packet(self, packet):
        session = self._current(mutable=True)
        session.require_ready()
        session.enqueue(packet, fail_session=False)

    def set_input(self, input):
        self._current(mutable=True).set_input(input)

    def set_look(self, pitch, yaw):
        self._current(mutable=True).set_look(pitch, yaw)

    def clear_look(self):
        self._current(mutable=True).look = None

    def player_slot(self, index):
        return self._current().player_slot(index)

    def get_item(self, slot):
        return self._current().get_item(slot)

    def get_player_inventory(self):
        s = self._current()
        return [s.get_item(s.player_slot(i)) for i in range(36)]

    def count_player_item(self, identifier):
        return self._current().count_player_item(identifier)

    def get_trade_window(self):
        return self._current().get_trade_window()

    def get_trade_offers(self, window: WindowRef) -> TradeOffers | None:
        return self._current().get_trade_offers(window)

    def begin_move_item(self, source: InventorySlot, destination: InventorySlot, count: int) -> ItemOperation:
        return self._current(mutable=True).begin_move_item(source, destination, count)

    def request_move_item(self, source: InventorySlot, destination: InventorySlot, count: int) -> ItemRequestKey:
        return self.begin_move_item(source, destination, count).key

    def begin_trade(self, offer: TradeOfferRef, destination: InventorySlot) -> ItemOperation:
        return self._current(mutable=True).begin_trade(offer, destination)

    def request_trade(self, offer: TradeOfferRef, destination: InventorySlot) -> ItemRequestKey:
        return self.begin_trade(offer, destination).key

    def trade_input(self, window, index):
        return self._current().trade_input(window, index)

    def select_hotbar(self, slot):
        self._current(mutable=True).select_hotbar(slot)

    def interact_actor(self, entity, hit_position=Vec3()):
        self._current(mutable=True).interact(entity, hit_position, False)

    def attack_actor(self, entity, hit_position=Vec3()):
        self._current(mutable=True).interact(entity, hit_position, True)

    def close_container(self, window):
        self._current(mutable=True).close_container(window)

    def cancel_item_operation(self, operation):
        return self._current(mutable=True).cancel_operation(operation)

    def request_chunk_radius(self, radius):
        self._current(mutable=True).request_chunk_radius(radius)

    def report_blob_status(self, missing, present=()):
        self._guard(mutable=True)
        from .session import bounded_ints

        missing = bounded_ints(missing, 0, (1 << 64) - 1, 524288)
        present = bounded_ints(present, 0, (1 << 64) - 1, 524288 - len(missing))
        self._current(mutable=True).report_blob_status(missing, present)

    def request_sub_chunks(self, dimension, chunk_x, chunk_z, heights):
        self._guard(mutable=True)
        from .session import bounded_ints

        heights = bounded_ints(heights, -128, 127, 1398101)
        self._current(mutable=True).request_sub_chunks(dimension, chunk_x, chunk_z, heights)

    def respawn(self, *, retry: bool = False):
        self._current(mutable=True).respawn(retry=retry)

    def probe_latency(self):
        self._current(mutable=True).probe_latency()
