import asyncio
from dataclasses import FrozenInstanceError
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from endpilot import Bot
from endpilot.item_use import ItemConsumption, ItemCooldownEvent
from endpilot.protocol import read_batch, write_batch


class ItemUseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("ItemUser", protocol=2225)
        self.b = self.bot.protocol.binding
        self.sent = asyncio.Queue()
        self.send_gate = None
        self.send_error = None
        self.addAsyncCleanup(self.bot.close)

        async def send(batch):
            for raw in read_batch(batch):
                if raw.packet_id == self.b.InventoryTransactionPacket.ID:
                    self.sent.put_nowait(self.bot.protocol.decode(raw).variant_transaction)
                    if self.send_gate is not None:
                        await self.send_gate.wait()
                    if self.send_error is not None:
                        raise self.send_error

        self.send = send
        await self.join()

    async def join(self, *before_spawn, before_game=()):
        self.incoming = asyncio.Queue()
        transport = SimpleNamespace(send=self.send, recv=self.incoming.get, close=AsyncMock())
        b = self.b
        self.enqueue(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.enqueue(*before_game, b.StartGamePacket(runtime_id=1, entity_id=2), *before_spawn,
                     b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await self.bot.join_nethernet("http://127.0.0.1:19152")
        self.item_use = self.bot._item_use
        await self.deliver(self.slot_update(0, 3))

    async def sync_position(self):
        b = self.b
        await self.deliver(b.MovePlayerPacket(player_id=1, pos=b.Vec3(x=0.5, y=74.62, z=0.5)))

    def enqueue(self, *packets):
        self.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, packets)))

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        waiter = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.enqueue(*packets, marker)
        await waiter

    def stack(self, count):
        return self.b.SerializedNetworkItemStackDescriptor(id=261, stack_size=count, user_data_buffer=b"")

    def slot_update(self, slot, count):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY,
                                          slot=slot, item=self.stack(count))

    def using(self, value, player_id=1):
        b = self.b
        return b.SetActorDataPacket(id=player_id, packed_items=b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=(1 << int(b.ActorFlags.USINGITEM)) if value else 0)),
        ]))

    async def test_login_keeps_last_valid_own_using_flag_in_receive_order(self):
        b = self.b
        missing = b.SetActorDataPacket(id=1)
        other_respawn = b.RespawnPacket(runtime_id=2, state=b.PlayerRespawnState.READY_TO_SPAWN)
        await self.bot.close()
        await self.join(self.using(False, player_id=2), missing, other_respawn, before_game=(self.using(True),))
        self.assertIs(self.bot.using_item, True)

        await self.bot.close()
        await self.join(self.using(True), self.using(False), self.using(True, player_id=2), missing)
        self.assertIs(self.bot.using_item, False)

    async def test_login_using_observation_respects_dimension_and_own_respawn_order(self):
        b = self.b
        resets = (b.ChangeDimensionPacket(dimension_id=1),
                  b.RespawnPacket(runtime_id=0, state=b.PlayerRespawnState.READY_TO_SPAWN),
                  b.RespawnPacket(runtime_id=1, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        for reset in resets:
            with self.subTest(reset=type(reset).__name__, runtime_id=getattr(reset, "runtime_id", None)):
                await self.bot.close()
                await self.join(self.using(True), reset, self.using(True, player_id=2), b.SetActorDataPacket(id=1))
                self.assertIsNone(self.bot.using_item)

                await self.bot.close()
                await self.join(self.using(True), reset, self.using(False))
                self.assertIs(self.bot.using_item, False)

    async def test_rejoin_without_own_flag_remains_unknown_until_a_live_report(self):
        await self.deliver(self.using(True))
        self.assertIs(self.bot.using_item, True)
        await self.bot.close()
        await self.join(self.using(False, player_id=2), self.b.SetActorDataPacket(id=1))
        self.assertIsNone(self.bot.using_item)
        await self.deliver(self.using(False))
        self.assertIs(self.bot.using_item, False)

    def consumption_transaction(self, slot=0, *, use=True, off_hand=False):
        b = self.b
        kind = b.ItemReleaseInventoryTransaction
        action = b.InventoryAction(
            source=b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY,
                                     container_id=b.ContainerID.INVENTORY),
            slot=slot, from_item_descriptor=self.stack(3), to_item_descriptor=self.stack(2),
        )
        return b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=kind(transaction=b.InventoryTransaction(actions=[action]), slot=slot,
                                     item=self.stack(3), hand=b.HandSlot.OFFHAND if off_hand else b.HandSlot.MAINHAND,
                                     action_type=kind.ActionType.USE if use else kind.ActionType.RELEASE),
        )

    async def test_waits_for_the_next_cooldown_and_filters_exact_categories(self):
        packet = self.b.PlayerStartItemCooldownPacket
        await self.deliver(packet(item_category="minecraft:chorusfruit", duration_ticks=99))
        matching = asyncio.create_task(self.item_use.wait_for_cooldown(category="minecraft:chorusfruit"))
        unfiltered = asyncio.create_task(self.item_use.wait_for_cooldown())
        self.addAsyncCleanup(self.cancel_task, matching)
        self.addAsyncCleanup(self.cancel_task, unfiltered)
        await asyncio.sleep(0)
        self.assertFalse(matching.done())
        self.assertFalse(unfiltered.done())
        await self.deliver(packet(item_category="chorusfruit", duration_ticks=7))
        self.assertEqual(await unfiltered, ItemCooldownEvent("chorusfruit", 7))
        self.assertFalse(matching.done())
        await self.deliver(packet(item_category="minecraft:chorusfruit", duration_ticks=20))
        event = await matching
        self.assertEqual(event, ItemCooldownEvent("minecraft:chorusfruit", 20))
        with self.assertRaises(FrozenInstanceError):
            event.duration_ticks = 10

    async def test_use_and_consumption_packets_do_not_supply_cooldown_announcements(self):
        await self.sync_position()
        waiting = asyncio.create_task(self.item_use.wait_for_cooldown())
        self.addAsyncCleanup(self.cancel_task, waiting)
        await asyncio.sleep(0)
        await self.bot.use_item()
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)
        await self.deliver(
            self.using(True), self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1),
            self.consumption_transaction(),
        )
        self.assertFalse(waiting.done())
        await self.deliver(self.b.PlayerStartItemCooldownPacket(item_category="minecraft:sample", duration_ticks=16))
        self.assertEqual(await waiting, ItemCooldownEvent("minecraft:sample", 16))

    async def test_cooldown_timeout_and_cancel_leave_the_packet_reader_usable(self):
        with self.assertRaises(TimeoutError):
            await self.item_use.wait_for_cooldown(timeout=0.01)
        waiting = asyncio.create_task(self.item_use.wait_for_cooldown(timeout=None))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        later = asyncio.create_task(self.item_use.wait_for_cooldown())
        self.addAsyncCleanup(self.cancel_task, later)
        await asyncio.sleep(0)
        await self.deliver(self.b.PlayerStartItemCooldownPacket(item_category="new", duration_ticks=19))
        self.assertEqual(await later, ItemCooldownEvent("new", 19))

    async def test_cooldown_wait_disconnects_even_with_a_matching_trailing_packet(self):
        waiting = asyncio.create_task(self.item_use.wait_for_cooldown(timeout=None))
        self.addAsyncCleanup(self.cancel_task, waiting)
        await asyncio.sleep(0)
        self.enqueue(self.b.DisconnectPacket(), self.b.PlayerStartItemCooldownPacket(item_category="late", duration_ticks=20))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        with self.assertRaises(ConnectionError):
            await self.item_use.wait_for_cooldown(timeout=0)

    async def test_bot_close_ends_cooldown_waits(self):
        waiting = asyncio.create_task(self.item_use.wait_for_cooldown(timeout=None))
        self.addAsyncCleanup(self.cancel_task, waiting)
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)

    async def test_requires_completion_for_the_held_item_and_its_use_transaction(self):
        b = self.b
        await self.sync_position()
        consuming = asyncio.create_task(self.bot.consume(timeout=2))
        self.addAsyncCleanup(self.cancel_task, consuming)
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), b.ItemUseInventoryTransaction)
        await self.deliver(self.using(True, player_id=34), self.slot_update(0, 2))
        self.assertIsNone(self.bot.using_item)
        self.assertFalse(consuming.done())
        await self.deliver(self.using(True), b.CompletedUsingItemPacket(item_id=430, item_use_method=1),
                           self.consumption_transaction())
        self.assertTrue(self.bot.using_item)
        self.assertFalse(consuming.done())
        await self.deliver(b.CompletedUsingItemPacket(item_id=261, item_use_method=1), self.slot_update(0, 2),
                           self.consumption_transaction(slot=5), self.consumption_transaction(use=False))
        self.assertFalse(consuming.done())

        await self.deliver(self.consumption_transaction(), self.using(False))
        self.assertEqual(await consuming, ItemConsumption(0, 261, "eat", 261, 2))
        self.assertFalse(self.bot.using_item)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 2)

    async def test_release_submits_without_predicting_a_false_flag_or_slot_change(self):
        b = self.b
        await self.sync_position()
        await self.bot.use_item()
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), b.ItemUseInventoryTransaction)
        await self.deliver(self.using(True))
        self.assertIsNone(await self.bot.release_using_item())
        release = await asyncio.wait_for(self.sent.get(), 1)
        self.assertIsInstance(release, b.ItemReleaseInventoryTransaction)
        self.assertEqual((release.action_type, release.slot, release.hand, release.item.id),
                         (b.ItemReleaseInventoryTransaction.ActionType.RELEASE, 0, b.HandSlot.MAINHAND, 261))
        self.assertTrue(self.bot.using_item)
        await self.deliver(self.using(False))
        self.assertFalse(self.bot.using_item)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 3)

    async def start_consuming(self):
        consuming = asyncio.create_task(self.bot.consume(timeout=2))
        self.addAsyncCleanup(self.cancel_task, consuming)
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)
        return consuming

    async def assert_consuming(self, consuming):
        done, _ = await asyncio.wait((consuming,), timeout=0.02)
        self.assertNotIn(consuming, done)

    async def test_observed_early_release_frees_consumption_for_reuse(self):
        await self.sync_position()
        consuming = await self.start_consuming()
        await self.deliver(self.using(True))
        self.assertIsNone(await self.bot.release_using_item())
        release = await asyncio.wait_for(self.sent.get(), 1)
        self.assertEqual(release.action_type, self.b.ItemReleaseInventoryTransaction.ActionType.RELEASE)
        await self.deliver(self.using(False, player_id=34),
                           self.b.PlayerStartItemCooldownPacket(item_category="minecraft:chorusfruit", duration_ticks=20),
                           self.b.CompletedUsingItemPacket(item_id=430, item_use_method=1))
        await self.assert_consuming(consuming)

        await self.deliver(self.using(False))
        with self.assertRaisesRegex(RuntimeError, "^item use was released before consumption was confirmed$"):
            await asyncio.wait_for(consuming, 0.25)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 3)

        consuming = await self.start_consuming()
        await self.deliver(self.using(True), self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1),
                           self.consumption_transaction(), self.using(False))
        self.assertEqual(await consuming, ItemConsumption(0, 261, "eat", 261, 2))

    async def test_completion_before_release_retains_wait_for_matching_transaction(self):
        await self.sync_position()
        consuming = await self.start_consuming()
        await self.deliver(self.using(True), self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1))
        self.assertIsNone(await self.bot.release_using_item())
        await self.deliver(self.using(False), self.consumption_transaction(off_hand=True),
                           self.consumption_transaction(slot=1))
        await self.assert_consuming(consuming)

        await self.deliver(self.consumption_transaction())
        self.assertEqual(await asyncio.wait_for(consuming, 0.25), ItemConsumption(0, 261, "eat", 261, 2))

    async def test_release_waits_for_transport_success_when_stop_arrives_during_write(self):
        await self.sync_position()
        for write_succeeds in (False, True):
            with self.subTest(write_succeeds=write_succeeds):
                consuming = await self.start_consuming()
                await self.deliver(self.using(True))
                self.send_gate = asyncio.Event()
                releasing = asyncio.create_task(self.bot.release_using_item())
                self.addAsyncCleanup(self.cancel_task, releasing)
                await asyncio.wait_for(self.sent.get(), 1)
                await self.deliver(self.using(False))
                await self.assert_consuming(consuming)
                self.assertFalse(releasing.done())
                if not write_succeeds:
                    self.send_error = OSError("release write failed")
                self.send_gate.set()

                if write_succeeds:
                    self.assertIsNone(await releasing)
                    with self.assertRaisesRegex(RuntimeError, "item use was released"):
                        await asyncio.wait_for(consuming, 0.25)
                else:
                    with self.assertRaisesRegex(OSError, "release write failed"):
                        await releasing
                    await self.assert_consuming(consuming)
                    consuming.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await consuming
                    self.send_error = None
                self.send_gate = None

    async def test_false_without_own_start_cannot_confirm_release(self):
        await self.sync_position()
        consuming = await self.start_consuming()
        await self.deliver(self.using(True, player_id=34))
        await self.bot.release_using_item()
        await self.deliver(self.using(False))
        await self.assert_consuming(consuming)

    async def test_release_before_use_write_cannot_claim_later_stop(self):
        await self.sync_position()
        packets = self.bot.packets
        send = packets.send
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def delayed_use(packet, **options):
            if (isinstance(packet, self.b.InventoryTransactionPacket)
                    and isinstance(packet.variant_transaction, self.b.ItemUseInventoryTransaction)):
                entered.set()
                await gate.wait()
            await send(packet, **options)

        with patch.object(packets, "send", delayed_use):
            consuming = asyncio.create_task(self.bot.consume(timeout=2))
            self.addAsyncCleanup(self.cancel_task, consuming)
            await asyncio.wait_for(entered.wait(), 1)
            await self.bot.release_using_item()
            self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemReleaseInventoryTransaction)
            gate.set()
            self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)
            await self.deliver(self.using(True), self.using(False))
            await self.assert_consuming(consuming)

    async def test_release_delayed_at_write_cannot_follow_replaced_consumption(self):
        await self.sync_position()
        old = await self.start_consuming()
        await self.deliver(self.using(True))
        packets = self.bot.packets
        send = packets.send
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def delayed_release(packet, **options):
            if (isinstance(packet, self.b.InventoryTransactionPacket)
                    and isinstance(packet.variant_transaction, self.b.ItemReleaseInventoryTransaction)):
                entered.set()
                await gate.wait()
            await send(packet, **options)

        with patch.object(packets, "send", delayed_release):
            releasing = asyncio.create_task(self.bot.release_using_item())
            self.addAsyncCleanup(self.cancel_task, releasing)
            await asyncio.wait_for(entered.wait(), 1)
            await self.cancel_task(old)
            await self.deliver(self.using(False))
            current = await self.start_consuming()
            await self.deliver(self.using(True))
            gate.set()
            self.assertIsNone(await releasing)
            self.assertTrue(self.sent.empty())
            await self.deliver(self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1),
                               self.consumption_transaction(), self.using(False))
            self.assertEqual(await current, ItemConsumption(0, 261, "eat", 261, 2))

    async def test_unowned_release_binds_before_waiting_for_position(self):
        await self.sync_position()
        movement = self.bot._movement
        sync = movement.wait_for_position_sync
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def delay_first_sync(*args, **options):
            if not entered.is_set():
                entered.set()
                await gate.wait()
            return await sync(*args, **options)

        with patch.object(movement, "wait_for_position_sync", delay_first_sync):
            releasing = asyncio.create_task(self.bot.release_using_item())
            self.addAsyncCleanup(self.cancel_task, releasing)
            await asyncio.wait_for(entered.wait(), 1)
            consuming = await self.start_consuming()
            await self.deliver(self.using(True))
            gate.set()
            self.assertIsNone(await releasing)
            self.assertTrue(self.sent.empty())
            await self.deliver(self.using(False))
            await self.assert_consuming(consuming)

    async def test_slot_change_after_release_cannot_confirm_stop(self):
        await self.sync_position()
        consuming = await self.start_consuming()
        await self.deliver(self.using(True))
        await self.bot.release_using_item()
        await self.bot.select_hotbar(1)
        await self.bot.select_hotbar(0)
        await self.deliver(self.using(False))
        await self.assert_consuming(consuming)
        await self.deliver(self.consumption_transaction())
        with self.assertRaisesRegex(RuntimeError, "selected slot changed"):
            await consuming

    async def test_close_interrupts_consume_while_release_is_in_flight(self):
        await self.sync_position()
        consuming = await self.start_consuming()
        await self.deliver(self.using(True))
        self.send_gate = asyncio.Event()
        releasing = asyncio.create_task(self.bot.release_using_item())
        self.addAsyncCleanup(self.cancel_task, releasing)
        await asyncio.wait_for(self.sent.get(), 1)
        await self.deliver(self.using(False))
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(consuming, 0.25)
        self.send_gate.set()
        self.assertIsNone(await releasing)

    async def test_timeout_preserves_slot_and_allows_another_submission(self):
        await self.sync_position()
        with self.assertRaises(TimeoutError):
            await self.bot.consume(timeout=0.02)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 3)
        await asyncio.wait_for(self.sent.get(), 1)
        await self.bot.use_item()
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)

    async def test_offhand_use_cannot_confirm_a_mainhand_consumption(self):
        await self.sync_position()
        consuming = asyncio.create_task(self.bot.consume(timeout=2))
        self.addAsyncCleanup(self.cancel_task, consuming)
        await asyncio.wait_for(self.sent.get(), 1)
        await self.deliver(self.using(True), self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1),
                           self.consumption_transaction(off_hand=True))
        done, _ = await asyncio.wait((consuming,), timeout=0.02)
        self.assertNotIn(consuming, done)

        await self.deliver(self.consumption_transaction())
        self.assertEqual(await consuming, ItemConsumption(0, 261, "eat", 261, 2))

    async def test_switching_away_and_back_invalidates_the_first_completion(self):
        await self.sync_position()
        consuming = asyncio.create_task(self.bot.consume(timeout=2))
        self.addAsyncCleanup(self.cancel_task, consuming)
        await asyncio.wait_for(self.sent.get(), 1)
        await self.deliver(self.using(True), self.b.CompletedUsingItemPacket(item_id=261, item_use_method=1))
        await self.bot.select_hotbar(1)
        await self.bot.select_hotbar(0)
        await self.deliver(self.consumption_transaction())
        with self.assertRaisesRegex(RuntimeError, "selected slot changed"):
            await consuming

    async def test_another_players_respawn_preserves_own_using_and_consumption(self):
        await self.sync_position()
        b = self.b
        consuming = asyncio.create_task(self.bot.consume(timeout=2))
        self.addAsyncCleanup(self.cancel_task, consuming)
        await asyncio.wait_for(self.sent.get(), 1)
        await self.deliver(self.using(True), b.RespawnPacket(runtime_id=34, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        self.assertTrue(self.bot.using_item)
        self.assertFalse(consuming.done())
        await self.deliver(b.CompletedUsingItemPacket(item_id=261, item_use_method=1), self.consumption_transaction())
        self.assertEqual(await consuming, ItemConsumption(0, 261, "eat", 261, 2))

    async def test_own_respawn_clears_using_and_interrupts_consumption(self):
        await self.sync_position()
        b = self.b
        for runtime_id in (0, 1):
            with self.subTest(runtime_id=runtime_id):
                consuming = asyncio.create_task(self.bot.consume(timeout=2))
                self.addAsyncCleanup(self.cancel_task, consuming)
                await asyncio.wait_for(self.sent.get(), 1)
                await self.deliver(self.using(True), b.RespawnPacket(
                    runtime_id=runtime_id, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN,
                ))
                with self.assertRaises(ConnectionError):
                    await consuming
                self.assertIsNone(self.bot.using_item)
                if runtime_id == 0:
                    await self.bot.submit_respawn(timeout=1)
                    self.enqueue(b.RespawnPacket(
                        pos=b.Vec3(x=0.5, y=74.62, z=0.5),
                        runtime_id=0, state=b.PlayerRespawnState.READY_TO_SPAWN,
                    ))
                    await self.bot.respawn(timeout=1)

    async def test_timeout_includes_waiting_for_initial_position(self):
        consuming = asyncio.create_task(self.bot.consume(timeout=0.02))
        self.addAsyncCleanup(self.cancel_task, consuming)
        done, _ = await asyncio.wait((consuming,), timeout=0.25)
        self.assertIn(consuming, done)
        with self.assertRaises(TimeoutError):
            await consuming
        self.assertTrue(self.sent.empty())

        await self.sync_position()
        await self.bot.use_item()
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)

    async def test_timeout_includes_sending_the_use_transaction(self):
        await self.sync_position()
        self.send_gate = asyncio.Event()
        consuming = asyncio.create_task(self.bot.consume(timeout=0.02))
        self.addAsyncCleanup(self.cancel_task, consuming)
        self.assertIsInstance(await asyncio.wait_for(self.sent.get(), 1), self.b.ItemUseInventoryTransaction)
        done, _ = await asyncio.wait((consuming,), timeout=0.25)
        self.assertIn(consuming, done)
        with self.assertRaises(TimeoutError):
            await consuming

    @staticmethod
    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
