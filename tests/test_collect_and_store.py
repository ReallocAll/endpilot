import asyncio
import math
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.block_data import _catalog
from endpilot.blocks import HASHED_AIR_RUNTIME_ID
from endpilot.collect_and_store import collect_and_store
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession

from test_collection import ORE, PLAYER, SECOND_TARGET, TARGET, Wire
from test_container_take import item
from test_scheduled_input import ManualClock


CHEST = (-4, 100, 3)
FAR_CHEST = (-9, 100, 3)
IRON_PICKAXE, DIAMOND, OTHER = 445, 446, 447
CHEST_ID = 2
CHEST_CONTAINER = "LEVEL_ENTITY_CONTAINER"
PLAYER_CONTAINER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"


class CollectAndStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CollectAndStore", protocol=2225)
        protocol = self.bot.protocol
        self.b = b = protocol.binding
        self.wire = Wire(protocol)
        packets = PacketConnection(self.wire, protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        session = JoinSession(packets, self.bot.identity, "127.0.0.1")
        session.player_id = session.player_unique_id = PLAYER
        session.block_network_ids_are_hashes = True
        registry = b.ItemRegistryPacket(items=[
            b.ItemData(id=IRON_PICKAXE, name="minecraft:iron_pickaxe"),
            b.ItemData(id=DIAMOND, name="minecraft:diamond"),
            b.ItemData(id=OTHER, name="minecraft:dirt"),
        ])
        session._replace_item_registry(registry)
        session.entity_packets.append(protocol.encode(registry))
        session._item_registry_subscriptions = (packets.subscribe_raw(session._on_item_registry),)
        self.join = self.enterContext(patch.object(JoinSession, "connect_raknet", return_value=session))
        await self.bot.join_raknet("127.0.0.1")
        packets.start_receiving()
        self.addAsyncCleanup(self.bot.close)
        palette, hashes = _catalog()
        self.chest_hash = next(runtime for runtime, index in hashes.items()
                               if palette[index] is not None and palette[index].name == "minecraft:chest")
        await self.emit(
            b.MovePlayerPacket(player_id=PLAYER, pos=b.Vec3(x=-2.5, y=101.62, z=3.5),
                               rot=b.Vec2(x=0, y=0), on_ground=True, reset_position=0),
            *(self.block((x, y, z), HASHED_AIR_RUNTIME_ID)
              for x in range(-5, 2) for y in range(99, 104) for z in range(1, 6)),
            self.block((-3, 99, 3), 0x80310E21),
            self.block((-3, 100, 3), HASHED_AIR_RUNTIME_ID, layer=1),
            self.block((-3, 101, 3), HASHED_AIR_RUNTIME_ID, layer=1),
            self.block(TARGET, ORE), self.block(SECOND_TARGET, ORE), self.block(CHEST, self.chest_hash),
            self.contents(0, PLAYER_CONTAINER, 36, {1: item(b, IRON_PICKAXE, 1, 11), 2: item(b, DIAMOND, 5, 12)}),
        )

    async def emit(self, *packets):
        self.wire.emit(*packets)
        await asyncio.sleep(0)

    def block(self, position, runtime_id, *, layer=0):
        x, y, z = position
        return self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer)

    def contents(self, window, container, width, stacks):
        b = self.b
        return b.InventoryContentPacket(
            inventory_id=window, slots=[stacks.get(index, item(b)) for index in range(width)],
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[container]), storage_item=item(b),
        )

    async def outgoing(self, kind, *, timeout=1):
        async with asyncio.timeout(timeout):
            while True:
                packet = await self.wire.outgoing.get()
                if isinstance(packet, kind):
                    return packet

    def start(self, positions=(TARGET, SECOND_TARGET), *, item_key=DIAMOND, chest_slots=None,
              chest=CHEST, ticks=0, search_radius=0, timeout=1, on_progress=None):
        self.progress = []
        task = asyncio.create_task(collect_and_store(
            self.bot, positions, item=item_key, chest=chest, chest_slots=chest_slots,
            ticks=ticks, search_radius=search_radius, timeout=timeout,
            on_progress=self.progress.append if on_progress is None else on_progress,
        ))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    async def begin_target(self, position):
        frame = await self.outgoing(self.b.PlayerAuthInputPacket)
        action, = frame.player_block_actions
        self.assertEqual(action.player_action_type.name, "START_DESTROY_BLOCK")
        self.assertEqual((action.pos.x, action.pos.y, action.pos.z), position)

    def item_actor(self, position, actor_id, *, item_id=DIAMOND):
        b = self.b
        x, y, z = position
        return b.AddItemActorPacket(id=actor_id + 1000, runtime_id=actor_id, item=item(b, item_id, 1),
                                    pos=b.Vec3(x=x + 0.5, y=y + 0.5, z=z + 0.5))

    def target_packets(self, position, actor_id, *, item_id=DIAMOND, total=None):
        b = self.b
        packets = [self.block(position, HASHED_AIR_RUNTIME_ID), self.item_actor(position, actor_id, item_id=item_id)]
        if total is not None:
            slot = 2 if item_id == DIAMOND else 3
            packets.extend((b.TakeItemActorPacket(item_id=actor_id, actor_id=PLAYER), b.InventorySlotPacket(
                inventory_id=0, slot=slot, item=item(b, item_id, total, 12 if slot == 2 else 13),
            )))
        return packets

    async def finish_target(self, position, actor_id, *, item_id=DIAMOND, total=None):
        await self.emit(*self.target_packets(position, actor_id, item_id=item_id, total=total))
        frame = await self.outgoing(self.b.PlayerAuthInputPacket)
        self.assertEqual([action.player_action_type.name for action in frame.player_block_actions],
                         ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])

    async def change_dimension_after_pickup(self, position, actor_id, *, total, ambiguous=False):
        b = self.b
        unrelated = (self.item_actor((1, 100, 4), 900), b.TakeItemActorPacket(item_id=900, actor_id=PLAYER))
        await self.emit(
            *(unrelated if ambiguous else ()),
            *self.target_packets(position, actor_id, total=total),
            b.ChangeDimensionPacket(dimension_id=1),
            self.block(CHEST, self.chest_hash),
            b.InventorySlotPacket(inventory_id=0, slot=2, item=item(b, DIAMOND, 9, 12)),
        )

    def assert_no_new_world_actions(self):
        b = self.b
        self.assertNotIn("approaching", [progress.phase for progress in self.progress])
        while not self.wire.outgoing.empty():
            packet = self.wire.outgoing.get_nowait()
            self.assertNotIsInstance(packet, (b.InventoryTransactionPacket, b.ItemStackRequestPacket))
            if isinstance(packet, b.PlayerAuthInputPacket):
                self.assertNotIn("START_DESTROY_BLOCK", [
                    action.player_action_type.name for action in packet.player_block_actions or ()
                ])

    async def open_chest(self, *, position=CHEST, count=0):
        b = self.b
        x, y, z = position
        stack = {} if not count else {0: item(b, DIAMOND, count, 21)}
        await self.emit(
            b.ContainerOpenPacket(container_id=CHEST_ID, type=b.ContainerType.CONTAINER,
                                  pos=b.BlockPos(x=x, y=y, z=z), entity_unique_id=-1),
            self.contents(CHEST_ID, CHEST_CONTAINER, 27, stack),
        )

    async def expect_click(self, position=CHEST, *, timeout=1):
        click = (await self.outgoing(self.b.InventoryTransactionPacket, timeout=timeout)).variant_transaction
        self.assertEqual((click.pos.x, click.pos.y, click.pos.z), position)
        self.assertEqual(click.face, 5)
        return click

    async def expect_store(self, amount, *, destination=0):
        request, = (await self.outgoing(self.b.ItemStackRequestPacket)).requests
        action, = request.actions
        self.assertEqual((action.source.slot, action.destination.slot, action.amount), (2, destination, amount))
        return request

    async def reply(self, request, *, player_count, chest_count, destination=0):
        b = self.b
        await self.emit(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[group]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=count, item_stack_net_id=b.ItemStackNetId(raw_id=net_id),
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                )],
            ) for group, slot, count, net_id in (
                (PLAYER_CONTAINER, 2, player_count, 12), (CHEST_CONTAINER, destination, chest_count, 21),
            )],
        )]))

    async def expect_close(self, *, acknowledge=True):
        packet = await self.outgoing(self.b.ContainerClosePacket)
        self.assertEqual(packet.container_id, CHEST_ID)
        if acknowledge:
            await self.emit(packet)
        return packet

    async def collect_two(self):
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.begin_target(SECOND_TARGET)
        await self.finish_target(SECOND_TARGET, 102, total=7)
        await self.expect_click()
        self.assertEqual((self.progress[-1].phase, self.progress[-1].picked_up_count), ("opening", 2))

    async def prepare_far_chest(self):
        await self.emit(
            *(self.block((x, y, z), 0x80310E21 if y == 99 else HASHED_AIR_RUNTIME_ID)
              for x in range(-10, -2) for z in range(2, 5) for y in range(99, 103)),
            *(self.block((x, y, z), HASHED_AIR_RUNTIME_ID, layer=1)
              for x in range(-10, -2) for z in range(2, 5) for y in (100, 101)),
            self.block(FAR_CHEST, self.chest_hash),
        )

    async def test_two_ore_pickups_store_only_the_new_quantity(self):
        task = self.start(item_key="minecraft:diamond")
        await self.collect_two()
        await self.open_chest()
        await self.reply(await self.expect_store(2), player_count=5, chest_count=2)
        close = await self.expect_close(acknowledge=False)
        self.assertEqual(self.bot.container_slots[0].stack_size, 2)
        self.assertEqual((self.progress[-1].phase, self.progress[-1].stored_count), ("closing", 2))
        await self.emit(close)
        result = await task
        self.assertIs(result, self.progress[-1])
        self.assertEqual((result.approach.status, result.approach.frames, result.approach.face), ("reached", 0, 5))
        self.assertEqual((result.phase, result.picked_up_count, result.stored_count, result.stop_reason),
                         ("finished", 2, 2, "complete"))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 5)
        self.assertIsNone(self.bot.open_window)
        self.join.assert_awaited_once()

    async def test_walks_to_the_distant_chest_before_clicking_the_arrival_face(self):
        await self.prepare_far_chest()
        task = self.start((TARGET,), chest=FAR_CHEST, ticks=160, search_radius=8, timeout=10)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        frame = await self.outgoing(self.b.PlayerAuthInputPacket)
        self.assertIn(self.b.PlayerAuthInputPacket.InputData.UP, frame.input_data)
        self.assertEqual((self.progress[-1].phase, self.progress[-1].picked_up_count), ("approaching", 1))
        click = await self.expect_click(FAR_CHEST, timeout=10)
        self.assertEqual((math.floor(click.from_pos.x), math.floor(click.from_pos.z)), (-8, 3))
        await self.open_chest(position=FAR_CHEST)
        await self.reply(await self.expect_store(1), player_count=5, chest_count=1)
        await self.expect_close()
        result = await task
        self.assertGreater(result.approach.frames, 0)
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (1, 1, "complete"))

    async def test_chest_outside_the_approach_bounds_keeps_the_pickup_without_clicking(self):
        await self.prepare_far_chest()
        task = self.start((TARGET,), chest=FAR_CHEST, ticks=160, search_radius=2)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        result = await task
        self.assertEqual(result.approach.status, "unreachable")
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (1, 0, "unreachable"))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 6)
        self.assertTrue(self.wire.outgoing.empty())
        self.assertEqual(await self.bot.walk_forward(1), 1)
        self.join.assert_awaited_once()

    async def test_position_correction_after_arrival_prevents_the_click(self):
        b = self.b

        def on_progress(progress):
            if progress.phase == "opening":
                self.wire.emit(b.MovePlayerPacket(
                    player_id=PLAYER, pos=b.Vec3(x=8.5, y=101.62, z=3.5), rot=b.Vec2(x=0, y=0),
                    on_ground=True, reset_position=0,
                ))

        task = self.start((TARGET,), on_progress=on_progress)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        result = await task
        self.assertEqual(result.approach.status, "reached")
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason),
                         (1, 0, "position_changed"))
        while not self.wire.outgoing.empty():
            self.assertNotIsInstance(self.wire.outgoing.get_nowait(),
                                     (b.InventoryTransactionPacket, b.ItemStackRequestPacket))

    async def test_chest_replacement_before_click_stops_without_using_the_new_block(self):
        def on_progress(progress):
            if progress.phase == "opening":
                self.wire.emit(self.block(CHEST, 0x80310E21))

        task = self.start((TARGET,), on_progress=on_progress)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        result = await task
        self.assertEqual(result.approach.status, "reached")
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason),
                         (1, 0, "chest_changed"))
        b = self.b
        while not self.wire.outgoing.empty():
            self.assertNotIsInstance(self.wire.outgoing.get_nowait(),
                                     (b.InventoryTransactionPacket, b.ItemStackRequestPacket))

    async def test_cancel_while_approaching_releases_movement_and_sends_no_click(self):
        await self.prepare_far_chest()
        task = self.start((TARGET,), chest=FAR_CHEST, ticks=160, search_radius=8, timeout=5)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        frame = await self.outgoing(self.b.PlayerAuthInputPacket)
        up = self.b.PlayerAuthInputPacket.InputData.UP
        self.assertIn(up, frame.input_data)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        progress = caught.exception.collect_and_store_progress
        self.assertEqual((progress.phase, progress.picked_up_count, progress.stored_count, progress.stop_reason),
                         ("approaching", 1, 0, "cancelled"))
        remaining = []
        while not self.wire.outgoing.empty():
            remaining.append(self.wire.outgoing.get_nowait())
        self.assertEqual(len(remaining), 2)
        for packet in remaining:
            self.assertIsInstance(packet, self.b.PlayerAuthInputPacket)
            self.assertNotIn(up, packet.input_data)
        self.assertEqual(await self.bot.walk_forward(1), 1)
        self.join.assert_awaited_once()

    async def test_full_allowed_stack_keeps_the_confirmed_partial_transfer(self):
        task = self.start(chest_slots=range(1))
        await self.collect_two()
        await self.open_chest(count=63)
        await self.reply(await self.expect_store(1), player_count=6, chest_count=64)
        close = await self.expect_close(acknowledge=False)
        self.assertEqual(self.bot.container_slots[0].stack_size, 64)
        await self.emit(close)
        result = await task
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (2, 1, "destination_full"))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 6)

    async def test_different_pickup_does_not_store_preexisting_diamonds(self):
        task = self.start((TARGET,))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, item_id=OTHER, total=1)
        result = await task
        self.assertEqual(result.collection.results[0].status, "collected")
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (0, 0, "no_matching_pickups"))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 5)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_dimension_change_keeps_a_same_batch_confirmed_pickup_without_storing(self):
        task = self.start()
        await self.begin_target(TARGET)
        await self.change_dimension_after_pickup(TARGET, 101, total=6)
        result = await task
        attempt, = result.collection.results
        self.assertEqual((attempt.status, attempt.inventory_delta, attempt.pickup_confirmed),
                         ("dimension_changed", 1, True))
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (1, 0, "dimension_changed"))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 9)
        self.assert_no_new_world_actions()

    async def test_dimension_change_does_not_count_an_ambiguous_same_item_gain(self):
        task = self.start()
        await self.begin_target(TARGET)
        await self.change_dimension_after_pickup(TARGET, 101, total=6, ambiguous=True)
        result = await task
        attempt, = result.collection.results
        self.assertEqual((attempt.status, attempt.collector_id, attempt.inventory_delta), ("dimension_changed", PLAYER, 1))
        self.assertEqual(len(attempt.drops), 1)
        self.assertFalse(attempt.pickup_confirmed)
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (0, 0, "dimension_changed"))
        self.assert_no_new_world_actions()

    async def test_dimension_change_keeps_the_completed_prefix_when_last_pickup_is_ambiguous(self):
        task = self.start()
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.begin_target(SECOND_TARGET)
        await self.change_dimension_after_pickup(SECOND_TARGET, 102, total=7, ambiguous=True)
        result = await task
        first, last = result.collection.results
        self.assertEqual((first.status, first.inventory_delta, first.pickup_confirmed), ("collected", 1, True))
        self.assertEqual((last.status, last.inventory_delta, last.pickup_confirmed), ("dimension_changed", 1, False))
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (1, 0, "dimension_changed"))
        self.assert_no_new_world_actions()

    async def test_collection_stop_stores_the_prefix_in_a_borrowed_window(self):
        await self.open_chest()
        borrowed = self.bot.open_window
        task = self.start((TARGET, (20, 100, 3)))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.reply(await self.expect_store(1), player_count=5, chest_count=1)
        result = await task
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason), (1, 1, "target_unobserved"))
        self.assertEqual(result.transfer.stop_reason, "complete")
        self.assertIs(self.bot.open_window, borrowed)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_remapped_item_id_cannot_store_the_new_ids_preexisting_stock(self):
        task = self.start((TARGET,))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        b = self.b
        await self.emit(
            b.ItemRegistryPacket(items=[b.ItemData(id=OTHER, name="minecraft:diamond")]),
            b.InventorySlotPacket(inventory_id=0, slot=3, item=item(b, OTHER, 5, 13)),
        )
        await self.open_chest()
        await self.expect_close()
        result = await task
        self.assertEqual((result.picked_up_count, result.stored_count, result.stop_reason),
                         (1, 0, "item_registry_changed"))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_cancel_collection_keeps_prefix_and_same_bot_can_collect_again(self):
        task = self.start()
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.begin_target(SECOND_TARGET)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        progress = caught.exception.collect_and_store_progress
        self.assertIs(progress.collection, caught.exception.collection_progress)
        self.assertEqual((progress.phase, progress.picked_up_count, progress.stored_count, progress.stop_reason),
                         ("collecting", 1, 0, "cancelled"))
        stop = await self.outgoing(self.b.PlayerAuthInputPacket)
        self.assertEqual([action.player_action_type.name for action in stop.player_block_actions],
                         ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])

        again = self.start((SECOND_TARGET,))
        await self.begin_target(SECOND_TARGET)
        await self.finish_target(SECOND_TARGET, 102, total=7)
        await self.expect_click()
        await self.open_chest()
        await self.reply(await self.expect_store(1), player_count=6, chest_count=1)
        await self.expect_close()
        result = await again
        self.assertEqual((result.picked_up_count, result.stored_count), (1, 1))
        self.join.assert_awaited_once()

    async def test_cancel_before_the_transfer_reply_preserves_the_unconfirmed_step(self):
        task = self.start(chest_slots=range(2))
        await self.collect_two()
        await self.open_chest(count=63)
        await self.reply(await self.expect_store(1), player_count=6, chest_count=64)
        late = await self.expect_store(1, destination=1)
        task.cancel()
        close = await self.expect_close(acknowledge=False)
        await self.reply(late, player_count=5, chest_count=1, destination=1)
        self.assertEqual(self.bot.inventory_count(DIAMOND), 5)
        self.assertEqual(self.bot.container_slots[1].stack_size, 1)
        await self.emit(close)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        progress = caught.exception.collect_and_store_progress
        self.assertIs(progress.transfer, caught.exception.transfer_progress)
        self.assertEqual((progress.phase, progress.picked_up_count, progress.stored_count, progress.stop_reason),
                         ("storing", 2, 1, "cancelled"))
        self.assertEqual([step.confirmed for step in progress.transfer.steps], [1, None])
        self.assertIsNone(self.bot.open_window)

    async def test_cancel_after_the_reader_applies_the_transfer_reply_keeps_the_confirmation(self):
        task = self.start((TARGET,))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        await self.open_chest()
        request = await self.expect_store(1)
        applied = []

        def cancel_on_reply(raw):
            if raw.packet_id == self.b.ItemStackResponsePacket.ID:
                applied.append((self.bot.inventory_count(DIAMOND), self.bot.container_slots[0].stack_size))
                task.cancel("reply already applied")

        unsubscribe = self.bot.packets.subscribe_raw(cancel_on_reply)
        try:
            await self.reply(request, player_count=5, chest_count=1)
            await self.expect_close()
        finally:
            unsubscribe()
        with self.assertRaisesRegex(asyncio.CancelledError, "reply already applied") as caught:
            await task
        self.assertEqual(applied, [(5, 1)])
        progress = caught.exception.collect_and_store_progress
        self.assertIs(progress.transfer, caught.exception.transfer_progress)
        self.assertEqual((progress.phase, progress.picked_up_count, progress.stored_count, progress.stop_reason),
                         ("storing", 1, 1, "cancelled"))
        step, = progress.transfer.steps
        self.assertEqual((step.request_id, step.verdict, step.confirmed), (request.client_request_id.raw_id, "SUCCESS", 1))
        self.assertIsNone(self.bot.open_window)

    async def test_cancel_at_closing_still_submits_and_waits_for_the_owned_close(self):
        def on_progress(progress):
            if progress.phase == "closing":
                task.cancel("first stop")

        task = self.start((TARGET,), on_progress=on_progress)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        await self.open_chest()
        await self.reply(await self.expect_store(1), player_count=5, chest_count=1)
        close = await self.expect_close(acknowledge=False)
        self.assertFalse(task.done())
        task.cancel("repeat stop while waiting")
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.emit(close)
        with self.assertRaisesRegex(asyncio.CancelledError, "first stop") as caught:
            await task
        progress = caught.exception.collect_and_store_progress
        self.assertEqual((progress.phase, progress.stored_count, progress.stop_reason), ("closing", 1, "cancelled"))
        self.assertIsNone(self.bot.open_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_cancel_at_closing_leaves_a_replacement_with_the_same_window_id_open(self):
        b = self.b

        def on_progress(progress):
            if progress.phase == "closing":
                x, y, z = CHEST
                self.wire.emit(b.ContainerOpenPacket(
                    container_id=CHEST_ID, type=b.ContainerType.CONTAINER,
                    pos=b.BlockPos(x=x, y=y, z=z), entity_unique_id=-1,
                ))
                task.cancel("replace then stop")

        task = self.start((TARGET,), on_progress=on_progress)
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        await self.open_chest()
        original = self.bot.open_window
        await self.reply(await self.expect_store(1), player_count=5, chest_count=1)
        with self.assertRaisesRegex(asyncio.CancelledError, "replace then stop") as caught:
            await task
        replacement = self.bot.open_window
        self.assertEqual(replacement, original)
        self.assertIsNot(replacement, original)
        self.assertEqual(caught.exception.collect_and_store_progress.stored_count, 1)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_cancel_opening_settles_and_closes_its_window(self):
        task = self.start((TARGET,))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        task.cancel()
        await self.open_chest()
        await self.expect_close()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        progress = caught.exception.collect_and_store_progress
        self.assertEqual((progress.phase, progress.picked_up_count, progress.stored_count, progress.stop_reason),
                         ("opening", 1, 0, "cancelled"))
        self.assertIsNone(self.bot.open_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_replacement_with_same_window_id_is_left_open(self):
        task = self.start((TARGET,))
        await self.begin_target(TARGET)
        await self.finish_target(TARGET, 101, total=6)
        await self.expect_click()
        await self.open_chest()
        request = await self.expect_store(1)
        original = self.bot.open_window
        await self.open_chest()
        replacement = self.bot.open_window
        self.assertEqual(replacement, original)
        self.assertIsNot(replacement, original)
        await self.reply(request, player_count=5, chest_count=1)
        await task
        self.assertIs(self.bot.open_window, replacement)
        self.assertTrue(self.wire.outgoing.empty())
