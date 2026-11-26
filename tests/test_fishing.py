import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import read_batch, write_batch


PLAYER = 42
OWNER = -4294967295
OTHER_OWNER = -4294967291
ROD, COD = 301, 302


class BatchTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.uses = asyncio.Queue()
        self.requests = asyncio.Queue()
        self.use_error = None

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in packets))

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def send(self, data):
        b = self.protocol.binding
        for raw in read_batch(data):
            if raw.packet_id == b.InventoryTransactionPacket.ID:
                action = self.protocol.decode(raw).variant_transaction
                if isinstance(action, b.ItemUseInventoryTransaction):
                    self.uses.put_nowait(action)
                    if self.use_error is not None:
                        raise self.use_error
            elif raw.packet_id == b.ItemStackRequestPacket.ID:
                self.requests.put_nowait(self.protocol.decode(raw).requests[0])

    async def close(self):
        pass


class FishingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Angler", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.transport = BatchTransport(self.bot.protocol)
        b = self.b
        self.transport.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.transport.deliver(
            b.StartGamePacket(entity_id=OWNER, runtime_id=PLAYER),
            b.ItemRegistryPacket(items=[b.ItemData(id=ROD, name="minecraft:fishing_rod"),
                                        b.ItemData(id=COD, name="minecraft:cod")]),
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY,
                                     slots=[self.item(ROD)] + [b.SerializedNetworkItemStackDescriptor()] * 35),
            b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        await self.deliver(b.MovePlayerPacket(player_id=PLAYER, pos=b.Vec3(x=5.5, y=101.62, z=4.5)))

    def item(self, item_id, count=1):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count)

    def hook(self, runtime, owner=OWNER, *, x=5.5, unique=None):
        b = self.b
        return b.AddActorPacket(
            entity_id=-runtime if unique is None else unique, runtime_id=runtime, actor_type="minecraft:fishing_hook",
            pos=b.Vec3(x=x, y=100, z=13.5), data=b.SynchedActorData.CopyableDataList(data=[
                b.DataItemEntry(id=int(b.ActorDataIDs.OWNER), payload=b.DataItemInt64Payload(value=owner)),
            ]),
        )

    def bite(self, runtime):
        return self.b.ActorEventPacket(runtime_id=runtime, event_id=self.b.ActorEvent.FISHHOOK_HOOKTIME)

    def drop(self, runtime, *, x=5.5, from_fishing=True):
        return self.b.AddItemActorPacket(
            id=-runtime, runtime_id=runtime, item=self.item(COD),
            pos=self.b.Vec3(x=x, y=100.1, z=13.5), is_from_fishing=from_fishing,
        )

    def update_inventory(self, count):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY, slot=1,
                                          item=self.item(COD, count))

    async def deliver(self, *packets):
        observed = asyncio.create_task(self.bot.packets.wait_for_packet(packets[-1].ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.deliver(*packets)
        await observed

    async def after_use(self, action):
        task = asyncio.create_task(action)

        async def finish():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(finish)
        use = await asyncio.wait_for(self.transport.uses.get(), timeout=1)
        self.assertEqual((use.slot, use.item.id), (0, ROD))
        return task

    async def test_owns_hooks_by_unique_id_and_only_accepts_that_hooks_bite(self):
        cast = await self.after_use(self.bot.cast_rod(timeout=1))
        # Matching the player's runtime ID in the OWNER field is insufficient.
        await self.deliver(self.hook(8, PLAYER, x=8.5), self.bite(8))
        self.assertIsNone(self.bot.fishing_hook)
        self.assertFalse(cast.done())
        await self.deliver(self.hook(9))
        first = await cast
        self.assertEqual((first.runtime_id, first.owner_entity_id), (9, OWNER))

        early = await self.after_use(self.bot.reel_rod(timeout=0.05))
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        empty = await early
        self.assertEqual((empty.status, empty.retracted, empty.received_count), ("empty_reel", True, None))
        self.assertTrue(any(actor.runtime_id == 8 for actor in self.bot.observed_entities))

        recast = await self.after_use(self.bot.cast_rod(timeout=1))
        await self.deliver(self.hook(10))
        self.assertEqual((await recast).runtime_id, 10)
        bite = asyncio.create_task(self.bot.wait_for_fishing_bite(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.bite(8), self.bite(9))
        self.assertFalse(bite.done())
        await self.deliver(self.bite(10))
        self.assertEqual((await bite).runtime_id, 10)

    async def test_received_result_requires_one_new_fishing_item_and_exact_personal_pickup(self):
        await self.deliver(self.hook(9), self.bite(9), self.drop(20))
        reel = await self.after_use(self.bot.reel_rod(timeout=0.05))
        await self.deliver(
            self.b.RemoveActorPacket(entity_id=-9), self.drop(20), self.drop(21, from_fishing=False),
            self.drop(22, x=8.5), self.drop(24),
            self.b.TakeItemActorPacket(item_id=22, actor_id=71),
            self.b.TakeItemActorPacket(item_id=24, actor_id=PLAYER), self.update_inventory(1),
        )
        result = await reel
        self.assertEqual((result.status, result.bite_observed, result.inventory_delta, result.received_count),
                         ("received", True, 1, 1))
        caught, = result.drops
        self.assertEqual((caught.runtime_id, caught.item_id, caught.is_from_fishing), (24, "minecraft:cod", True))
        self.assertEqual(result.pickups, ((24, PLAYER),))

    async def test_reel_does_not_select_between_multiple_drops_or_competing_reels(self):
        for runtime, competing in ((9, False), (29, True)):
            with self.subTest(competing=competing):
                await self.deliver(self.hook(runtime), self.bite(runtime))
                if competing:
                    await self.deliver(self.hook(8, OTHER_OWNER, x=8.5))
                reel = await self.after_use(self.bot.reel_rod(timeout=0.05))
                alternative = self.b.RemoveActorPacket(entity_id=-8) if competing else self.drop(runtime + 2)
                await self.deliver(
                    self.b.RemoveActorPacket(entity_id=-runtime), self.drop(runtime + 1), alternative,
                    self.b.TakeItemActorPacket(item_id=runtime + 1, actor_id=PLAYER),
                    self.update_inventory(1 if runtime == 9 else 2),
                )
                result = await reel
                self.assertEqual((result.status, result.received_count), ("ambiguous", None))
                self.assertEqual(result.pickups, ((runtime + 1, PLAYER),))

    async def test_timed_out_fishing_retracts_the_same_hook_before_another_cast(self):
        attempt = await self.after_use(self.bot.fish(bite_timeout=0.01, cast_timeout=1))
        await self.deliver(self.hook(9))
        cancel_use = await asyncio.wait_for(self.transport.uses.get(), timeout=1)
        self.assertEqual(cancel_use.item.id, ROD)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        with self.assertRaises(TimeoutError):
            await attempt
        self.assertIsNone(self.bot.fishing_hook)

        cast = await self.after_use(self.bot.cast_rod(timeout=1))
        await self.deliver(self.hook(10))
        self.assertEqual((await cast).runtime_id, 10)

    async def test_foreign_pickup_cannot_be_explained_by_our_inventory_gain(self):
        await self.deliver(self.hook(9), self.bite(9))
        reel = await self.after_use(self.bot.reel_rod(timeout=0.05))
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9), self.drop(10),
                           self.b.TakeItemActorPacket(item_id=10, actor_id=71), self.update_inventory(1))
        result = await reel
        self.assertEqual((result.status, result.pickups, result.received_count),
                         ("taken_by_other", ((10, 71),), None))

    async def test_personal_pickup_without_a_complete_baseline_remains_partial(self):
        await self.deliver(self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.INVENTORY,
                                                         slots=[self.item(ROD)]), self.hook(9), self.bite(9))
        reel = await self.after_use(self.bot.reel_rod(timeout=0.05))
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9), self.drop(10),
                           self.b.TakeItemActorPacket(item_id=10, actor_id=PLAYER), self.update_inventory(1))
        result = await reel
        self.assertEqual((result.status, result.pickups, result.inventory_delta, result.received_count),
                         ("inventory_unconfirmed", ((10, PLAYER),), None, None))

    async def test_server_approved_inventory_drop_after_a_pickup_clears_net_receipt(self):
        b = self.b
        await self.deliver(self.hook(9), self.bite(9))
        reel = await self.after_use(self.bot.reel_rod(timeout=0.25))
        await self.deliver(b.RemoveActorPacket(entity_id=-9), self.drop(10),
                           b.TakeItemActorPacket(item_id=10, actor_id=PLAYER), self.update_inventory(1))

        dropping = asyncio.create_task(self.bot.drop_inventory_item(1, timeout=1))
        request = await asyncio.wait_for(self.transport.requests.get(), timeout=1)
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName.HOTBAR_CONTAINER),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=1, slot=1, amount=0)],
            )],
        )]))
        self.assertEqual((await dropping).result, b.ItemStackNetResult.SUCCESS)
        self.assertEqual(self.bot.inventory_slots[1].id, 0)
        result = await reel
        self.assertEqual((result.status, result.pickups, result.inventory_delta, result.received_count),
                         ("inventory_unconfirmed", ((10, PLAYER),), 0, None))

    async def test_later_inventory_transaction_subtracts_from_the_personal_pickup(self):
        b = self.b
        await self.deliver(self.hook(9), self.bite(9))
        reel = await self.after_use(self.bot.reel_rod(timeout=0.1))
        await self.deliver(b.RemoveActorPacket(entity_id=-9), self.drop(10),
                           b.TakeItemActorPacket(item_id=10, actor_id=PLAYER), self.update_inventory(1))
        await self.deliver(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=[b.InventoryAction(
                source=b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY,
                                         container_id=b.ContainerID.INVENTORY), slot=1,
                from_item_descriptor=self.item(COD), to_item_descriptor=self.item(0, 0),
            )])),
        ))
        result = await reel
        self.assertEqual((result.pickups, result.inventory_delta, result.received_count), (((10, PLAYER),), 0, None))

    async def test_automatic_cleanup_and_explicit_reel_wait_for_one_late_removal(self):
        attempt = await self.after_use(self.bot.fish(bite_timeout=0.02, cast_timeout=0.2))
        await self.deliver(self.hook(9))
        await asyncio.wait_for(self.transport.uses.get(), timeout=1)
        with self.assertRaises(TimeoutError):
            await attempt

        result = await self.bot.reel_rod(timeout=0.03)
        self.assertEqual((result.status, result.retracted), ("reel_unconfirmed", False))
        self.assertTrue(self.transport.uses.empty())

        waiting = asyncio.create_task(self.bot.cancel_fishing(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        self.assertTrue(await waiting)
        self.assertTrue(self.transport.uses.empty())
        self.assertIsNone(self.bot.fishing_hook)
        next_cast = await self.after_use(self.bot.cast_rod(timeout=1))
        await self.deliver(self.hook(10))
        self.assertEqual((await next_cast).runtime_id, 10)

    async def test_cancelled_reel_cannot_resubmit_for_the_same_unique_hook(self):
        await self.deliver(self.hook(9))
        reel = await self.after_use(self.bot.reel_rod(timeout=1))
        reel.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reel
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9), self.hook(10, unique=-9))
        self.assertEqual((self.bot.fishing_hook.runtime_id, self.bot.fishing_hook.entity_id), (10, -9))
        repeated = asyncio.create_task(self.bot.reel_rod(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        result = await repeated
        self.assertEqual((result.status, result.retracted), ("reel_unconfirmed", True))
        self.assertTrue(self.transport.uses.empty())

    async def test_local_rejection_keeps_the_first_reel_available_until_transport_entry(self):
        await self.deliver(self.hook(9))
        with patch.object(self.bot._item_use, "_consuming", True):
            with self.assertRaisesRegex(RuntimeError, "consumption is already awaiting confirmation"):
                await self.bot.reel_rod(timeout=0.03)
        self.assertTrue(self.transport.uses.empty())

        encode = self.bot.protocol.encode

        def fail_use_encoding(packet):
            if (isinstance(packet, self.b.InventoryTransactionPacket)
                and isinstance(packet.variant_transaction, self.b.ItemUseInventoryTransaction)):
                raise ValueError("item use encoding failed")
            return encode(packet)

        with patch.object(self.bot.protocol, "encode", side_effect=fail_use_encoding):
            with self.assertRaisesRegex(ValueError, "item use encoding failed"):
                await self.bot.reel_rod(timeout=0.03)
        self.assertTrue(self.transport.uses.empty())

        reel = await self.after_use(self.bot.reel_rod(timeout=0.03))
        result = await reel
        self.assertEqual((result.status, result.retracted), ("reel_unconfirmed", False))
        waiting = asyncio.create_task(self.bot.cancel_fishing(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        self.assertTrue(await waiting)
        self.assertTrue(self.transport.uses.empty())

    async def test_dropped_rod_rejects_reel_until_slot_correction_without_using_an_attempt(self):
        b = self.b
        await self.deliver(self.hook(9))
        await self.bot.drop_selected_item(whole_stack=True)
        self.assertEqual(self.bot.inventory_slots[0].id, ROD)
        with self.assertRaisesRegex(ValueError, "select an observed fishing rod"):
            await self.bot.reel_rod(timeout=0.2)
        self.assertTrue(self.transport.uses.empty())

        await self.deliver(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0, item=self.item(ROD)))
        reel = await self.after_use(self.bot.reel_rod(timeout=1))
        await self.deliver(b.RemoveActorPacket(entity_id=-9))
        self.assertTrue((await reel).retracted)
        self.assertTrue(self.transport.uses.empty())

    async def test_uncertain_transport_delivery_keeps_the_reel_marked(self):
        await self.deliver(self.hook(9))
        self.transport.use_error = OSError("delivery unknown")
        with self.assertRaisesRegex(OSError, "delivery unknown"):
            await self.bot.reel_rod(timeout=0.03)
        self.assertEqual((await asyncio.wait_for(self.transport.uses.get(), timeout=1)).item.id, ROD)

        self.transport.use_error = None
        waiting = asyncio.create_task(self.bot.cancel_fishing(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-9))
        self.assertTrue(await waiting)
        self.assertTrue(self.transport.uses.empty())

    async def assert_session_ended(self):
        self.assertIsNone(self.bot.fishing_hook)
        for action in (self.bot.cast_rod, self.bot.wait_for_fishing_bite, self.bot.reel_rod,
                       self.bot.cancel_fishing, self.bot.fish):
            with self.subTest(action=action.__name__), self.assertRaises(ConnectionError):
                await asyncio.wait_for(action(), timeout=0.2)
        self.assertTrue(self.transport.uses.empty())

    async def test_disconnect_wins_over_matched_hook_and_later_packets_in_same_batch(self):
        cast = await self.after_use(self.bot.cast_rod(timeout=1))
        await self.deliver(self.hook(9), self.b.DisconnectPacket(), self.hook(10), self.bite(10))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(cast, timeout=0.2)
        await self.assert_session_ended()

    async def test_reader_failure_without_disconnect_invalidates_live_hook_and_waiters(self):
        await self.deliver(self.hook(9))
        self.assertEqual(self.bot.fishing_hook.runtime_id, 9)
        bite = asyncio.create_task(self.bot.wait_for_fishing_bite(timeout=15))
        await asyncio.sleep(0)
        self.transport.incoming.put_nowait(OSError("reader stopped"))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(bite, timeout=0.2)
        await self.assert_session_ended()
        with self.assertRaisesRegex(OSError, "reader stopped"):
            await self.bot.close()
