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
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.send_gate = None
        self.addAsyncCleanup(self.bot.close)

        async def send(batch):
            for raw in read_batch(batch):
                if raw.packet_id == self.b.InventoryTransactionPacket.ID:
                    self.sent.put_nowait(self.bot.protocol.decode(raw).variant_transaction)
                    if self.send_gate is not None:
                        await self.send_gate.wait()

        transport = SimpleNamespace(send=send, recv=self.incoming.get, close=AsyncMock())
        b = self.b
        self.enqueue(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.enqueue(b.StartGamePacket(runtime_id=1, entity_id=2),
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
        await self.bot.release_using_item()
        release = await asyncio.wait_for(self.sent.get(), 1)
        self.assertIsInstance(release, b.ItemReleaseInventoryTransaction)
        self.assertEqual((release.action_type, release.slot, release.hand, release.item.id),
                         (b.ItemReleaseInventoryTransaction.ActionType.RELEASE, 0, b.HandSlot.MAINHAND, 261))
        self.assertTrue(self.bot.using_item)
        await self.deliver(self.using(False))
        self.assertFalse(self.bot.using_item)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 3)

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
