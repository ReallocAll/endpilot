import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from endpilot import Bot, PlacementInventoryChange, PlacementResult
from endpilot.blocks import Blocks, HASHED_AIR_RUNTIME_ID
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.inventory import Inventory
from endpilot.protocol import write_batch
from test_scheduled_input import Wire


SUPPORT, TARGET = (4, 90, 7), (4, 91, 7)
SUPPORT_ID, PLACED_ID = 776, 777
FURNACE_ITEM_STATE, FURNACE_PLACED_STATE = 3463497305, 4038254352
STONE, SHORT_GRASS = 2150698529, 2827903781


class PlacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Placement", protocol=2225)
        protocol = self.bot.protocol
        self.b = protocol.binding
        self.wire = Wire(protocol)
        settings = self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE)
        self.packets = PacketConnection(self.wire, protocol, settings)
        self.movement = SimpleNamespace(
            _changing_dimension=False,
            input_position=(3.5, 92.62, 7.5),
            wait_for_position_sync=AsyncMock(return_value=(3.5, 92.62, 7.5)),
        )
        self.inventory = Inventory(self.packets, 7)
        self.containers = Containers(self.packets, self.movement, self.inventory)
        self.blocks = Blocks(self.packets, self.movement, self.inventory, hashed_runtime_ids=True)
        self.blocks.start()
        self.bot._containers = self.containers
        self.bot._blocks = self.blocks
        self.bot._creative = SimpleNamespace(game_mode=self.b.GameType.SURVIVAL)
        self.packets.start_receiving()
        self.addCleanup(self.blocks.close)
        self.addCleanup(self.containers.close)
        self.addCleanup(self.inventory.close)
        self.addAsyncCleanup(self.packets.close)
        await self.deliver(self.block(SUPPORT, SUPPORT_ID), self.block(TARGET), self.slot(4))

    def block(self, position, runtime_id=HASHED_AIR_RUNTIME_ID, layer=0):
        x, y, z = position
        return self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer)

    def slot(self, count, *, slot=0, item_id=308, net_id=21, block_runtime_id=PLACED_ID):
        item = self.b.SerializedNetworkItemStackDescriptor(
            id=item_id if count else 0, stack_size=count, net_id_variant=net_id if count else None,
            block_runtime_id=block_runtime_id if count else 0,
        )
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY, slot=slot, item=item)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=42)
        read = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await read

    async def place(self, timeout=0.6, **expected):
        return await self.bot.place_block(
            SUPPORT, face=1, click_pos=(0.25, 1, 0.75), timeout=timeout,
            **(expected or {"expected_runtime_id": PLACED_ID}),
        )

    async def arm(self, timeout=0.6, **expected):
        sent = asyncio.Event()
        stop = self.packets.subscribe_sent(lambda packet_id: sent.set() if packet_id == self.b.InventoryTransactionPacket.ID else None)
        pending = asyncio.create_task(self.place(timeout, **expected))
        self.addAsyncCleanup(self.stop_task, pending)
        try:
            await asyncio.wait_for(sent.wait(), 0.5)
        finally:
            stop()
        return pending

    @staticmethod
    async def stop_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_one_click_and_separate_observations_in_either_order(self):
        for world_first in (True, False):
            with self.subTest(world_first=world_first):
                await self.deliver(self.block(TARGET), self.slot(4))
                self.wire.sent.clear()
                pending = await self.arm()
                sent, = self.wire.sent
                tx = sent.variant_transaction
                self.assertEqual((tx.pos.x, tx.pos.y, tx.pos.z), SUPPORT)
                self.assertEqual((tx.click_pos.x, tx.click_pos.y, tx.click_pos.z), (0.25, 1, 0.75))
                self.assertEqual((tx.face, tx.slot, tx.target_block_id), (1, 0, SUPPORT_ID))
                self.assertEqual((tx.item.id, tx.item.net_id_variant, tx.item.stack_size), (308, 21, 4))
                self.assertEqual(tx.action_type, self.b.ItemUseInventoryTransaction.ActionType.PLACE)
                world, inventory = self.block(TARGET, PLACED_ID), self.slot(3, net_id=22)
                first, next_packet = (world, inventory) if world_first else (inventory, world)
                await self.deliver(first)
                self.assertFalse(pending.done())
                await self.deliver(next_packet)
                result = await pending
                self.assertIsInstance(result, PlacementResult)
                self.assertEqual((result.submitted, result.reason, result.target), (True, "observations_complete", TARGET))
                self.assertTrue(result.world_change.before.is_air)
                self.assertEqual(result.world_change.after.runtime_id, PLACED_ID)
                self.assertIsInstance(result.inventory_change, PlacementInventoryChange)
                self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (4, 3))
                self.assertEqual(result.inventory_change.after.net_id, 22)
                with self.assertRaises(FrozenInstanceError):
                    result.inventory_change.after.count = 12
                self.assertEqual(len(self.wire.sent), 1)
        self.assertFalse(self.blocks._block_observers)

    async def test_replace_short_grass_observes_the_clicked_cell_and_selected_slot(self):
        await self.deliver(self.block(SUPPORT, SHORT_GRASS), self.block(TARGET, FURNACE_PLACED_STATE),
                           self.slot(4, block_runtime_id=STONE))
        pending = await self.arm(replace=True, expected_block_name="minecraft:stone")
        sent, = self.wire.sent
        tx = sent.variant_transaction
        self.assertEqual((tx.pos.x, tx.pos.y, tx.pos.z), SUPPORT)
        self.assertEqual((tx.click_pos.x, tx.click_pos.y, tx.click_pos.z), (0.25, 1, 0.75))
        self.assertEqual((tx.face, tx.slot, tx.target_block_id), (1, 0, SHORT_GRASS))
        self.assertEqual(tx.action_type, self.b.ItemUseInventoryTransaction.ActionType.PLACE)

        await self.deliver(self.block(TARGET, STONE), self.block(SUPPORT, STONE, layer=1),
                           self.block(SUPPORT, FURNACE_PLACED_STATE), self.block(SUPPORT, STONE),
                           self.slot(3, slot=1, net_id=30, block_runtime_id=STONE))
        self.assertFalse(pending.done())
        await self.deliver(self.block(SUPPORT, SHORT_GRASS), self.block(SUPPORT, STONE))
        self.assertFalse(pending.done())
        await self.deliver(self.slot(3, net_id=22, block_runtime_id=STONE))

        result = await pending
        self.assertEqual((result.submitted, result.reason, result.target), (True, "observations_complete", SUPPORT))
        before, after = result.world_change.before, result.world_change.after
        self.assertEqual((before.position, before.layer, before.runtime_id), (SUPPORT, 0, SHORT_GRASS))
        self.assertEqual((after.position, after.layer, after.runtime_id), (SUPPORT, 0, STONE))
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (4, 3))
        self.assertEqual(result.inventory_change.after.net_id, 22)
        self.assertEqual(len(self.wire.sent), 1)

    async def test_replace_requires_known_short_grass_in_the_clicked_main_layer(self):
        unknown = (7, 90, 7)
        result = await self.bot.place_block(unknown, face=1, replace=True, expected_runtime_id=FURNACE_PLACED_STATE)
        self.assertEqual((result.submitted, result.reason, result.target), (False, "precondition_failed", unknown))

        for runtime_id in (SUPPORT_ID, STONE):
            with self.subTest(runtime_id=runtime_id):
                await self.deliver(self.block(SUPPORT, runtime_id), self.block(SUPPORT, SHORT_GRASS, layer=1))
                result = await self.place(replace=True, expected_runtime_id=FURNACE_PLACED_STATE)
                self.assertEqual((result.submitted, result.reason, result.target),
                                 (False, "precondition_failed", SUPPORT))
        self.assertEqual(self.wire.sent, [])

    async def test_default_placement_does_not_send_a_click_on_short_grass(self):
        await self.deliver(self.block(SUPPORT, SHORT_GRASS))
        result = await self.place()
        self.assertEqual((result.submitted, result.reason, result.target), (False, "precondition_failed", TARGET))
        self.assertEqual(self.wire.sent, [])

    async def test_name_matches_new_primary_state_but_integer_keeps_exact_state(self):
        for expected in ({"expected_block_name": "minecraft:furnace"}, {"expected_runtime_id": FURNACE_ITEM_STATE}):
            with self.subTest(expected=expected):
                slot_properties = {"item_id": 61, "block_runtime_id": FURNACE_ITEM_STATE}
                await self.deliver(self.block(TARGET), self.slot(2, **slot_properties))
                self.wire.sent.clear()
                pending = await self.arm(timeout=0.15, **expected)
                sent, = self.wire.sent
                self.assertEqual(sent.variant_transaction.item.block_runtime_id, FURNACE_ITEM_STATE)
                await self.deliver(self.block(TARGET, FURNACE_PLACED_STATE, layer=1), self.slot(1, **slot_properties))
                self.assertFalse(pending.done())
                await self.deliver(self.block(TARGET, FURNACE_PLACED_STATE))
                result = await pending
                self.assertTrue(result.submitted)
                self.assertEqual(result.inventory_change.after.count, 1)
                if "expected_block_name" in expected:
                    self.assertEqual(result.reason, "observations_complete")
                    self.assertEqual(result.world_change.after.runtime_id, FURNACE_PLACED_STATE)
                    self.assertEqual(result.world_change.after.layer, 0)
                else:
                    self.assertEqual(result.reason, "timed_out")
                    self.assertIsNone(result.world_change)

    async def test_name_requires_exact_known_state(self):
        for block_name, runtime_id in (("minecraft:stone", FURNACE_PLACED_STATE), ("minecraft:furnace", PLACED_ID)):
            with self.subTest(block_name=block_name, runtime_id=runtime_id):
                await self.deliver(self.block(TARGET), self.slot(4))
                pending = await self.arm(timeout=0.15, expected_block_name=block_name)
                await self.deliver(self.block(TARGET, runtime_id), self.slot(3))
                result = await pending
                self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
                self.assertIsNone(result.world_change)
                self.assertEqual(result.inventory_change.after.count, 3)

    async def test_name_and_runtime_id_are_mutually_exclusive(self):
        for expected in ({}, {"expected_runtime_id": PLACED_ID, "expected_block_name": "minecraft:furnace"}):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "specify exactly one"):
                await self.bot.place_block(SUPPORT, face=1, **expected)
        self.assertEqual(self.wire.sent, [])

    async def test_name_match_in_next_dimension_stays_outside_observation(self):
        pending = await self.arm(expected_block_name="minecraft:furnace")
        await self.deliver(self.slot(3), self.b.ChangeDimensionPacket(dimension_id=1),
                           self.block(TARGET), self.block(TARGET, FURNACE_PLACED_STATE))
        result = await pending
        self.assertEqual((result.dimension_id, result.submitted, result.reason), (0, True, "dimension_changed"))
        self.assertIsNone(result.world_change)
        self.assertEqual(result.inventory_change.after.count, 3)

    async def test_other_player_name_match_preserves_world_only(self):
        pending = await self.arm(timeout=0.15, expected_block_name="minecraft:furnace")
        await self.deliver(self.block(TARGET, FURNACE_PLACED_STATE), self.slot(4), self.slot(3, slot=1))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertEqual(result.world_change.after.runtime_id, FURNACE_PLACED_STATE)
        self.assertIsNone(result.inventory_change)

    async def test_other_player_match_does_not_fill_inventory_evidence(self):
        pending = await self.arm(timeout=0.15)
        await self.deliver(self.block(TARGET, PLACED_ID), self.slot(4), self.slot(3, slot=1), self.slot(3, item_id=33))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertEqual(result.world_change.after.runtime_id, PLACED_ID)
        self.assertIsNone(result.inventory_change)

    async def test_corrections_wrong_layer_and_non_air_transition_do_not_match(self):
        await self.deliver(self.slot(1))
        pending = await self.arm(timeout=0.15)
        await self.deliver(self.block(TARGET), self.block(TARGET, PLACED_ID, layer=1),
                           self.block(TARGET, SUPPORT_ID), self.block(TARGET, PLACED_ID), self.slot(0))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertIsNone(result.world_change)
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.item_id), (1, 0))

    async def test_selected_stack_or_target_change_before_submission_sends_zero(self):
        scenarios = (
            (False, self.slot(2)),
            (False, self.block(TARGET, PLACED_ID)),
            (True, self.block(SUPPORT, STONE)),
        )
        for replace, correction in scenarios:
            with self.subTest(replace=replace, packet=type(correction).__name__):
                await self.deliver(self.slot(4), self.block(TARGET),
                                   self.block(SUPPORT, SHORT_GRASS if replace else SUPPORT_ID))
                entering = asyncio.Event()
                ready = asyncio.Event()

                async def sync(*, timeout):
                    entering.set()
                    await ready.wait()

                self.movement.wait_for_position_sync.side_effect = sync
                pending = asyncio.create_task(self.place(replace=replace, expected_runtime_id=PLACED_ID))
                self.addAsyncCleanup(self.stop_task, pending)
                await asyncio.wait_for(entering.wait(), 0.5)
                await self.deliver(correction)
                ready.set()
                result = await pending
                self.assertEqual((result.submitted, result.reason), (False, "precondition_failed"))
                self.assertIsNone(result.world_change)
                self.assertIsNone(result.inventory_change)
        self.assertEqual(self.wire.sent, [])

    async def test_unknown_or_empty_baseline_and_zero_budget_do_not_submit(self):
        zero_budget = await self.place(timeout=0)
        self.assertEqual((zero_budget.submitted, zero_budget.reason), (False, "timed_out"))
        unknown = await self.bot.place_block((7, 90, 7), face=1, expected_runtime_id=PLACED_ID)
        self.assertEqual((unknown.submitted, unknown.reason), (False, "precondition_failed"))
        await self.deliver(self.slot(0))
        empty = await self.place()
        self.assertEqual((empty.submitted, empty.reason), (False, "precondition_failed"))
        self.assertEqual(self.wire.sent, [])

    async def test_send_failure_cannot_claim_events_received_while_writing(self):
        async def fail_send():
            await self.deliver(self.block(TARGET, PLACED_ID), self.slot(3))
            raise OSError("transport failed while writing")

        self.wire.before_send = fail_send
        result = await self.place()
        self.assertEqual((result.submitted, result.reason), (None, "send_failed"))
        self.assertIsNone(result.world_change)
        self.assertIsNone(result.inventory_change)
        self.assertEqual(self.wire.sent, [])

    async def test_unrelated_slot_cannot_reuse_selected_slot_change_from_before_commit(self):
        async def in_flight():
            await self.deliver(self.slot(3))

        self.wire.before_send = in_flight
        pending = await self.arm(timeout=0.15)
        await self.deliver(self.block(TARGET, PLACED_ID), self.slot(2, slot=7))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertEqual(result.world_change.after.runtime_id, PLACED_ID)
        self.assertIsNone(result.inventory_change)
        self.assertEqual(self.inventory.held_item.observed_item.count, 3)

    async def test_first_selected_slot_update_after_commit_uses_commit_time_baseline(self):
        async def in_flight():
            await self.deliver(self.slot(3, net_id=22))

        self.wire.before_send = in_flight
        pending = await self.arm()
        await self.deliver(self.slot(2, net_id=23), self.slot(1, net_id=24), self.block(TARGET, PLACED_ID))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "observations_complete"))
        change = result.inventory_change
        self.assertEqual((change.before.count, change.before.net_id), (3, 22))
        self.assertEqual((change.after.count, change.after.net_id), (2, 23))
        self.assertEqual(self.inventory.held_item.observed_item.count, 1)

    async def test_repeated_world_state_cannot_reuse_world_change_from_before_commit(self):
        async def in_flight():
            await self.deliver(self.block(TARGET, PLACED_ID))

        self.wire.before_send = in_flight
        pending = await self.arm(timeout=0.15)
        await self.deliver(self.block(TARGET, PLACED_ID), self.slot(3))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertIsNone(result.world_change)
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (4, 3))

    async def test_cancelled_caller_retains_original_error_and_partial_result(self):
        pending = await self.arm()
        await self.deliver(self.slot(3))
        pending.cancel("caller stopped waiting")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await pending
        self.assertTrue(pending.cancelled())
        self.assertIs(type(caught.exception), asyncio.CancelledError)
        self.assertEqual(str(caught.exception), "caller stopped waiting")
        progress = caught.exception.placement_progress
        self.assertEqual((progress.submitted, progress.reason), (True, "cancelled"))
        self.assertEqual(progress.inventory_change.after.count, 3)
        self.assertIsNone(progress.world_change)
        self.assertFalse(self.blocks._block_observers)

    async def test_unconfirmed_inflight_cancel_never_claims_submission(self):
        writing = asyncio.Event()

        async def write():
            writing.set()
            await asyncio.Future()

        self.wire.before_send = write
        pending = asyncio.create_task(self.place())
        self.addAsyncCleanup(self.stop_task, pending)
        await writing.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await pending
        self.assertIsNone(caught.exception.placement_progress.submitted)
        self.assertEqual(self.wire.sent, [])

    async def test_caller_cancel_during_transport_cleanup_preserves_progress(self):
        writing = asyncio.Event()
        cleaning = asyncio.Event()

        async def write():
            writing.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await asyncio.Future()

        self.wire.before_send = write
        pending = asyncio.create_task(self.place())
        self.addAsyncCleanup(self.stop_task, pending)
        await writing.wait()
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        await cleaning.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await pending
        progress = caught.exception.placement_progress
        self.assertEqual((progress.submitted, progress.reason), (None, "cancelled"))

    async def test_dimension_change_ends_window_before_new_dimension_evidence(self):
        pending = await self.arm()
        await self.deliver(self.slot(3), self.b.ChangeDimensionPacket(dimension_id=1), self.block(TARGET, PLACED_ID))
        result = await pending
        self.assertEqual((result.dimension_id, result.submitted, result.reason), (0, True, "dimension_changed"))
        self.assertIsNone(result.world_change)
        self.assertEqual(result.inventory_change.after.count, 3)

    async def test_disconnect_ends_window_and_preserves_received_world_evidence(self):
        pending = await self.arm()
        await self.deliver(self.block(TARGET, PLACED_ID), self.b.DisconnectPacket(), self.slot(3))
        result = await pending
        self.assertEqual((result.submitted, result.reason), (True, "disconnected"))
        self.assertEqual(result.world_change.after.runtime_id, PLACED_ID)
        self.assertIsNone(result.inventory_change)
