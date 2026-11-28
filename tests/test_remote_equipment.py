import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import ActorEquipmentSnapshot, Bot
from endpilot.compression import PacketConnection
from endpilot.entities import Entities
from endpilot.inventory import HeldItemStack
from endpilot.protocol import Protocol, RawPacket, write_batch
from test_scheduled_input import Wire


class RemoteEquipmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = b = self.protocol.binding
        self.wire = Wire(self.protocol)
        self.packets = PacketConnection(self.wire, self.protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.entities = Entities(self.packets, own_runtime_id=7, own_entity_id=107)
        self.entities.start()
        self.packets.start_receiving()
        self.addAsyncCleanup(self.packets.close)
        self.addCleanup(self.entities.close)

    def stack(self, item_id=0, count=1):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count if item_id else 0)

    def player(self, *, runtime=8, uuid=8, unique=108, carried=3):
        b = self.b
        return b.AddPlayerPacket(uuid=UUID(int=uuid), runtime_id=runtime, name=f"Remote{uuid}",
                                 carried_item=self.stack(carried),
                                 abilities_data=b.SerializedAbilitiesData(target_player=unique))

    def listed(self, *, uuid=8, unique=108):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=uuid), id=unique, name=f"Listed{uuid}",
        )])

    def held(self, item_id, slot, *, runtime=8, container=None):
        b = self.b
        return b.MobEquipmentPacket(runtime_id=runtime, slot=slot, selected_slot=slot, item=self.stack(item_id),
                                    container_id=b.ContainerID.INVENTORY if container is None else container)

    def armor(self, *, runtime=8, head=0, chest=0, legs=0, feet=0, body=0):
        return self.b.MobArmorEquipmentPacket(runtime_id=runtime, head=self.stack(head), torso=self.stack(chest),
                                              legs=self.stack(legs), feet=self.stack(feet), body=self.stack(body))

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        processed = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (*packets, marker))))
        await processed

    async def wait(self, runtime=8):
        pending = asyncio.create_task(self.entities.wait_for_player_equipment_change(runtime, timeout=None))
        self.addAsyncCleanup(self.cancel, pending)
        await asyncio.sleep(0)
        return pending

    async def wait_for_actor(self, runtime=8):
        pending = asyncio.create_task(self.entities.wait_for_actor_equipment_change(runtime, timeout=None))
        self.addAsyncCleanup(self.cancel, pending)
        await asyncio.sleep(0)
        return pending

    async def cancel(self, pending):
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    async def test_initial_holding_preserves_unknown_armor_and_ignores_self_nonplayers_and_pre_add_updates(self):
        b = self.b
        offhand = b.ContainerID.OFFHAND
        await self.deliver(self.armor(head=9), self.held(6, 1), self.held(10, 0, container=offhand),
                           self.player(), self.player(runtime=7, uuid=7),
                           b.AddActorPacket(runtime_id=9, entity_id=109, actor_type="minecraft:zombie"),
                           self.armor(runtime=7, head=10), self.armor(runtime=9, head=11),
                           self.held(10, 0, runtime=7, container=offhand),
                           self.held(11, 0, runtime=9, container=offhand))
        snapshot = self.entities.player_equipment(8)
        self.assertEqual((snapshot.runtime_id, snapshot.entity_id, snapshot.name), (8, 108, "Remote8"))
        self.assertEqual((snapshot.main_hand.item_id, snapshot.main_hand.count), (3, 1))
        self.assertIsNone(snapshot.selected_slot)
        self.assertIsNone(snapshot.head)
        self.assertIsNone(self.entities.player_equipment(7))
        self.assertIsNone(self.entities.player_equipment(9))
        self.assertIsNone(self.entities.get(8).last_observed_offhand)
        self.assertIsNone(self.entities.get(7))
        self.assertIsNone(self.entities.get(9).last_observed_offhand)
        with self.assertRaises(LookupError):
            await self.entities.wait_for_player_equipment_change(9)
        for target in (7, 9, 10):
            with self.subTest(target=target), self.assertRaises(LookupError):
                self.entities.subscribe_player_equipment_changes(self.fail, runtime_id=target)
        with self.assertRaises(FrozenInstanceError):
            snapshot.selected_slot = 1
        with self.assertRaises(FrozenInstanceError):
            snapshot.main_hand.count = 2

    async def test_atomic_four_slot_armour_and_actual_empty_slots(self):
        await self.deliver(self.player())
        unknown = self.entities.player_equipment(8)
        snapshots = []
        self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        self.assertEqual(snapshots, [])
        pending = await self.wait()
        await self.deliver(self.armor(head=10, chest=11, legs=12), self.armor(head=15))
        first = await pending
        self.assertIsNone(unknown.head)
        self.assertEqual((first.head.item_id, first.chest.item_id, first.legs.item_id, first.feet.item_id), (10, 11, 12, 0))
        self.assertEqual(first.feet.count, 0)
        newest = self.entities.player_equipment(8)
        self.assertEqual((newest.head.item_id, newest.chest.item_id, newest.legs.item_id, newest.feet.item_id), (15, 0, 0, 0))
        self.assertEqual(first.main_hand, newest.main_hand)
        self.assertEqual(snapshots, [first, newest])

    async def test_repeat_same_slot_and_descriptor_ignored_but_distinct_slot_and_empty_are_changes(self):
        await self.deliver(self.player(), self.held(3, 1), self.armor(head=6))
        observed = []

        def observe(snapshot):
            observed.append((snapshot, self.entities.player_equipment(8)))

        unsubscribe = self.entities.subscribe_player_equipment_changes(observe, runtime_id=8)
        pending = await self.wait()
        await self.deliver(self.player(runtime=10, uuid=10, unique=110), self.held(11, 2, runtime=10),
                           self.held(11, 2, runtime=7), self.held(11, 2, runtime=9),
                           self.held(3, 1), self.held(10, 2, container=self.b.ContainerID.OFFHAND),
                           self.armor(head=6, body=11))
        self.assertFalse(pending.done())
        self.assertEqual(observed, [])
        self.assertEqual(self.entities.get(8).last_observed_offhand.item_id, 10)
        equipment = self.entities.player_equipment(8)
        self.assertEqual((equipment.selected_slot, equipment.main_hand.item_id), (1, 3))
        await self.deliver(self.held(3, 2), self.held(0, 3))
        moved = await pending
        self.assertEqual((moved.selected_slot, moved.main_hand.item_id), (2, 3))
        now = self.entities.player_equipment(8)
        self.assertEqual((now.selected_slot, now.main_hand.item_id, now.main_hand.count), (3, 0, 0))
        self.assertEqual(observed, [(moved, moved), (now, now)])
        unsubscribe()
        unsubscribe()
        await self.deliver(self.held(12, 4))
        self.assertEqual(len(observed), 2)
        self.assertEqual(self.entities.player_equipment(8).main_hand.item_id, 12)
        self.assertEqual(self.wire.sent, [])

    async def test_subscription_errors_do_not_interrupt_subscribers_or_later_packets(self):
        await self.deliver(self.player())
        loop = asyncio.get_running_loop()
        errors = []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context))

        def fail(_):
            raise ValueError("handler failed")

        def cancel(_):
            raise asyncio.CancelledError("handler cancelled")

        self.entities.subscribe_player_equipment_changes(fail, runtime_id=8)
        self.entities.subscribe_player_equipment_changes(cancel, runtime_id=8)
        snapshots = []
        self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        await self.deliver(self.held(4, 1), self.held(5, 2))
        self.assertEqual([snapshot.main_hand.item_id for snapshot in snapshots], [4, 5])
        self.assertEqual([context["callback"] for context in errors], [fail, cancel, fail, cancel])
        self.assertEqual([type(context["exception"]) for context in errors],
                         [ValueError, asyncio.CancelledError, ValueError, asyncio.CancelledError])

    async def test_offhand_survives_motion_and_updates_to_an_observed_empty_stack(self):
        b = self.b
        await self.deliver(self.player())
        unknown = self.entities.get(8)
        await self.deliver(self.held(10, 0, container=b.ContainerID.OFFHAND))
        previous = self.entities.get(8)
        offhand = previous.last_observed_offhand
        self.assertEqual((offhand.item_id, offhand.count), (10, 1))

        await self.deliver(b.MovePlayerPacket(player_id=8, pos=b.Vec3(x=1, y=2, z=3)), self.held(12, 3),
                           b.InventorySlotPacket(inventory_id=b.ContainerID.OFFHAND, slot=0, item=self.stack(0)))
        current = self.entities.get(8)
        self.assertEqual(current.position, (1, 2, 3))
        self.assertEqual(current.last_observed_offhand, offhand)

        await self.deliver(self.held(0, 0, container=b.ContainerID.OFFHAND))
        self.assertEqual(self.entities.get(8).last_observed_offhand, HeldItemStack(0, 0, 0, None, 0, b""))
        self.assertIsNone(unknown.last_observed_offhand)
        self.assertEqual(previous.last_observed_offhand, offhand)
        with self.assertRaises(FrozenInstanceError):
            current.last_observed_offhand = None

    async def test_late_roster_enriches_identity_without_replacing_actor_or_resolving_a_gear_wait(self):
        await self.deliver(self.player(unique=0), self.armor(head=6),
                           self.held(10, 0, container=self.b.ContainerID.OFFHAND))
        self.assertIsNone(self.entities.player_equipment(8).entity_id)
        instance = self.entities._actor_instance(8)
        snapshots = []
        self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        pending = await self.wait()
        await self.deliver(self.b.RemoveActorPacket(entity_id=909), self.listed())
        self.assertIs(instance, self.entities._actor_instance(8))
        self.assertFalse(pending.done())
        self.assertEqual(snapshots, [])
        self.assertEqual(self.entities.player_equipment(8).head.item_id, 6)
        self.assertEqual(self.entities.get(8).last_observed_offhand.item_id, 10)
        await self.deliver(self.held(12, 1))
        changed = await pending
        self.assertEqual((changed.entity_id, changed.main_hand.item_id, changed.head.item_id), (108, 12, 6))
        self.assertEqual(snapshots, [changed])

    async def test_conflicting_roster_identity_discards_prior_offhand(self):
        offhand = self.b.ContainerID.OFFHAND
        await self.deliver(self.player(), self.held(10, 0, container=offhand))
        previous = self.entities.get(8)
        await self.deliver(self.listed(unique=109), self.held(11, 0, container=offhand))
        self.assertEqual(previous.last_observed_offhand.item_id, 10)
        current = self.entities.get(8)
        self.assertEqual(current.entity_id, 109)
        self.assertIsNone(current.last_observed_offhand)

    async def test_late_roster_cannot_rebind_an_unidentified_actor_removed_since_spawn(self):
        await self.deliver(self.player(unique=0), self.armor(head=6))
        prior = self.entities.player_equipment(8)
        self.assertIsNone(prior.entity_id)
        pending = await self.wait()
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.listed(), self.held(19, 1))
        self.assertIsNone(self.entities.player_equipment(8))
        self.assertIsNone(self.entities.get(8))
        self.assertEqual(self.entities.player_list()[0].entity_id, 108)
        self.assertEqual(prior.head.item_id, 6)
        with self.assertRaises(LookupError):
            await pending

    async def test_late_roster_applies_to_a_real_respawn_after_unmatched_removal(self):
        await self.deliver(self.player(unique=0), self.armor(head=6))
        old = await self.wait()
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.player(unique=0, carried=15))
        with self.assertRaises(LookupError):
            await old
        fresh = self.entities.player_equipment(8)
        self.assertIsNone(fresh.head)
        new = await self.wait()
        await self.deliver(self.listed())
        self.assertFalse(new.done())
        self.assertEqual(self.entities.player_equipment(8).entity_id, 108)
        await self.deliver(self.armor(head=9))
        changed = await new
        self.assertEqual((changed.entity_id, changed.main_hand.item_id, changed.head.item_id), (108, 15, 9))

    async def test_unidentified_actor_with_incomplete_removal_history_is_not_rebound(self):
        await self.deliver(self.player(unique=0))
        waiting = await self.wait()
        await self.deliver(self.b.RemoveActorPacket(entity_id=108),
                           *(self.b.RemoveActorPacket(entity_id=value) for value in range(300, 556)),
                           self.listed(), self.held(18, 1))
        self.assertIsNone(self.entities.player_equipment(8))
        with self.assertRaises(LookupError):
            await waiting

    async def test_removal_before_roster_and_reused_runtime_do_not_retain_equipment_or_waiter(self):
        b = self.b
        offhand = b.ContainerID.OFFHAND
        await self.deliver(self.player(), self.armor(head=6), self.held(10, 0, container=offhand))
        snapshots = []
        callback = snapshots.append
        removed = self.entities.subscribe_player_equipment_changes(callback, runtime_id=8)
        pending = await self.wait()
        await self.deliver(b.RemoveActorPacket(entity_id=108), self.held(99, 0),
                           self.held(11, 0, container=offhand), self.listed())
        self.assertIsNone(self.entities.player_equipment(8))
        self.assertIsNone(self.entities.get(8))
        with self.assertRaises(LookupError):
            await pending
        await self.deliver(self.player(uuid=9, unique=109, carried=15), b.RemoveActorPacket(entity_id=108))
        current = self.entities.player_equipment(8)
        self.assertEqual((current.entity_id, current.main_hand.item_id, current.head), (109, 15, None))
        self.assertIsNone(self.entities.get(8).last_observed_offhand)
        await self.deliver(self.held(4, 1))
        self.assertEqual(snapshots, [])
        self.entities.subscribe_player_equipment_changes(callback, runtime_id=8)
        removed()
        await self.deliver(self.held(5, 1))
        replacement = await self.wait()
        await self.deliver(self.held(12, 0, container=offhand), self.player(uuid=10, unique=110, carried=16),
                           self.armor(head=7))
        with self.assertRaises(LookupError):
            await replacement
        self.assertEqual((self.entities.player_equipment(8).name, self.entities.player_equipment(8).head.item_id),
                         ("Remote10", 7))
        self.assertIsNone(self.entities.get(8).last_observed_offhand)
        self.assertEqual([(snapshot.entity_id, snapshot.main_hand.item_id) for snapshot in snapshots], [(109, 5)])

    async def test_repeated_spawn_with_same_ids_ends_subscription(self):
        await self.deliver(self.player())
        snapshots = []
        self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        await self.deliver(self.player(carried=4), self.held(5, 1))
        self.assertEqual(snapshots, [])
        self.assertEqual(self.entities.player_equipment(8).main_hand.item_id, 5)

    async def test_dimension_departure_and_return_preserve_roster_only(self):
        await self.deliver(self.listed(), self.player(), self.armor(head=6))
        snapshots = []
        self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        waiting = await self.wait()
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.held(9, 1))
        self.assertEqual(self.entities.player_list()[0].entity_id, 108)
        self.assertIsNone(self.entities.player_equipment(8))
        with self.assertRaises(LookupError):
            await waiting
        await self.deliver(self.player(carried=8), self.armor(head=9))
        now = self.entities.player_equipment(8)
        self.assertEqual((now.main_hand.item_id, now.head.item_id), (8, 9))
        self.assertEqual(snapshots, [])

    async def test_timeout_cancel_and_session_close(self):
        await self.deliver(self.player())
        with self.assertRaises(TimeoutError):
            await self.entities.wait_for_player_equipment_change(8, timeout=0.01)
        cancelled = await self.wait()
        retained = await self.wait()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await self.deliver(self.held(4, 1))
        self.assertEqual((await retained).main_hand.item_id, 4)
        pending = await self.wait()
        self.entities.close()
        with self.assertRaises(ConnectionError):
            await pending
        self.assertIsNone(self.entities.player_equipment(8))
        with self.assertRaises(ConnectionError):
            await self.entities.wait_for_player_equipment_change(8)

    async def test_disconnect_ignores_trailing_equipment(self):
        await self.deliver(self.player())
        snapshots = []
        unsubscribe = self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)
        pending = await self.wait()
        await self.deliver(self.b.DisconnectPacket(), self.armor(head=11))
        with self.assertRaises(ConnectionError):
            await pending
        self.assertIsNone(self.entities.player_equipment(8))
        self.assertEqual(snapshots, [])
        unsubscribe()
        unsubscribe()
        with self.assertRaises(ConnectionError):
            self.entities.subscribe_player_equipment_changes(snapshots.append, runtime_id=8)

    async def test_actor_armor_is_atomic_and_repeat_reports_are_silent(self):
        b = self.b
        actor = b.AddActorPacket(runtime_id=8, entity_id=108, actor_type="minecraft:armor_stand")
        await self.deliver(self.armor(head=1), actor, self.player(runtime=9, uuid=9, unique=109))
        self.assertIsNone(self.entities.actor_equipment(8))
        state = self.entities.get(8)
        for target in (7, 9, 10):
            self.assertIsNone(self.entities.actor_equipment(target))
            with self.subTest(target=target), self.assertRaises(LookupError):
                self.entities.subscribe_actor_equipment_changes(self.fail, runtime_id=target)
        with self.assertRaises(LookupError):
            await self.entities.wait_for_actor_equipment_change(9)

        observed, reentrant = [], []

        def observe(snapshot):
            observed.append((snapshot, self.entities.actor_equipment(8)))
            unsubscribe()
            self.entities.subscribe_actor_equipment_changes(reentrant.append, runtime_id=8)

        unsubscribe = self.entities.subscribe_actor_equipment_changes(observe, runtime_id=8)
        first_wait = await self.wait_for_actor()
        armor = self.armor(head=10, chest=11, legs=12, feet=13, body=14)
        await self.deliver(self.armor(runtime=7, body=20), self.armor(runtime=9, head=6),
                           self.armor(runtime=10, head=7), armor)
        first = await first_wait
        self.assertIsInstance(first, ActorEquipmentSnapshot)
        self.assertEqual((first.runtime_id, first.entity_id, first.actor_type), (8, 108, "minecraft:armor_stand"))
        self.assertEqual((first.head.item_id, first.chest.item_id, first.legs.item_id, first.feet.item_id,
                          first.body.item_id), (10, 11, 12, 13, 14))
        self.assertIsNone(first.main_hand)
        self.assertEqual(observed, [(first, first)])
        self.assertEqual(reentrant, [])
        self.assertIs(self.entities.get(8), state)
        self.assertEqual(self.entities.player_equipment(9).head.item_id, 6)
        for target in (7, 9, 10):
            self.assertIsNone(self.entities.actor_equipment(target))

        next_wait = await self.wait_for_actor()
        await self.deliver(armor)
        self.assertFalse(next_wait.done())
        self.assertEqual(reentrant, [])
        await self.deliver(self.armor(chest=11))
        second = await next_wait
        self.assertEqual(second.head, HeldItemStack(0, 0, 0, None, 0, b""))
        self.assertEqual(second.chest, first.chest)
        self.assertEqual(second.body, second.head)
        self.assertEqual(first.body.item_id, 14)
        self.assertEqual(reentrant, [second])
        with self.assertRaises(FrozenInstanceError):
            first.head = None
        with self.assertRaises(FrozenInstanceError):
            first.body.count = 0
        unsubscribe()
        self.assertEqual(self.wire.sent, [])

    async def test_actor_main_hand_uses_only_zero_inventory_selectors(self):
        b = self.b
        actor = b.AddActorPacket(runtime_id=8, entity_id=108, actor_type="minecraft:zombie")
        await self.deliver(self.held(12, 0), actor)
        self.assertIsNone(self.entities.actor_equipment(8))
        changes = []
        unsubscribe = self.entities.subscribe_actor_equipment_changes(changes.append, runtime_id=8)
        initial = await self.wait_for_actor()
        await self.deliver(self.held(15, 0, container=b.ContainerID.OFFHAND), self.held(15, 1),
                           b.MobEquipmentPacket(runtime_id=8, slot=1, selected_slot=0,
                                                item=self.stack(15), container_id=b.ContainerID.INVENTORY),
                           b.MobEquipmentPacket(runtime_id=8, slot=0, selected_slot=1,
                                                item=self.stack(15), container_id=b.ContainerID.INVENTORY))
        self.assertIsNone(self.entities.actor_equipment(8))
        self.assertFalse(initial.done())
        self.assertEqual(changes, [])

        await self.deliver(self.held(0, 0), self.held(0, 0))
        empty = await initial
        self.assertEqual(empty.main_hand, HeldItemStack(0, 0, 0, None, 0, b""))
        self.assertIsNone(empty.head)
        self.assertEqual(changes, [empty])
        await self.deliver(self.held(309, 0), self.armor(head=346), self.held(0, 0))
        drawn, armored, cleared = changes[1:]
        self.assertEqual(drawn.main_hand.item_id, 309)
        self.assertIsNone(drawn.head)
        self.assertEqual(armored.main_hand, drawn.main_hand)
        self.assertEqual(armored.head.item_id, 346)
        self.assertEqual(cleared.head, armored.head)
        self.assertEqual(cleared.main_hand, empty.main_hand)
        self.assertIsNone(self.entities.get(8).last_observed_offhand)
        self.assertIsNone(self.entities.player_equipment(8))
        unsubscribe()
        self.assertEqual(self.wire.sent, [])

    async def test_actor_equipment_subscriptions_end_on_remove_replace_and_dimension_change(self):
        b = self.b
        actor = b.AddActorPacket(runtime_id=8, entity_id=108, actor_type="minecraft:horse")
        for transition in (b.RemoveActorPacket(entity_id=108), actor, b.ChangeDimensionPacket(dimension_id=1)):
            with self.subTest(transition=type(transition).__name__):
                await self.deliver(actor, self.armor(body=14))
                old = self.entities.actor_equipment(8)
                snapshots = []
                unsubscribe = self.entities.subscribe_actor_equipment_changes(snapshots.append, runtime_id=8)
                pending = await self.wait_for_actor()
                await self.deliver(transition)
                with self.assertRaises(LookupError):
                    await pending
                self.assertIsNone(self.entities.actor_equipment(8))
                if transition is not actor:
                    await self.deliver(self.armor(body=21), actor)
                    self.assertIsNone(self.entities.actor_equipment(8))
                replacement = self.entities.subscribe_actor_equipment_changes(snapshots.append, runtime_id=8)
                unsubscribe()
                await self.deliver(self.armor(body=15))
                self.assertEqual([snapshot.body.item_id for snapshot in snapshots], [15])
                self.assertEqual(old.body.item_id, 14)
                replacement()

    async def test_actor_equipment_timeout_cancel_and_reader_end(self):
        await self.deliver(self.b.AddActorPacket(runtime_id=8, entity_id=108, actor_type="minecraft:horse"))
        with self.assertRaises(TimeoutError):
            await self.entities.wait_for_actor_equipment_change(8, timeout=0)
        cancelled = await self.wait_for_actor()
        retained = await self.wait_for_actor()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await self.deliver(self.armor(body=14))
        self.assertEqual((await retained).body.item_id, 14)

        snapshots = []
        unsubscribe = self.entities.subscribe_actor_equipment_changes(snapshots.append, runtime_id=8)
        pending = await self.wait_for_actor()
        await self.packets.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(pending, 1)
        self.assertIsNone(self.entities.actor_equipment(8))
        self.assertEqual(snapshots, [])
        unsubscribe()
        with self.assertRaises(ConnectionError):
            self.entities.subscribe_actor_equipment_changes(snapshots.append, runtime_id=8)


class RemoteEquipmentStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_packets_preserve_wire_order_and_own_hotbar(self):
        bot = Bot("GearObserver", protocol=2225)
        self.addAsyncCleanup(bot.close)
        snapshots = []
        with self.assertRaises(RuntimeError):
            bot.subscribe_player_equipment_changes(snapshots.append, runtime_id=1)
        with self.assertRaises(RuntimeError):
            bot.observed_actor_equipment(8)
        protocol = bot.protocol
        b = protocol.binding
        wire = Wire(protocol)

        def deliver(*packets):
            raws = (packet if isinstance(packet, RawPacket) else protocol.encode(packet) for packet in packets)
            wire.incoming.put_nowait(write_batch(raws))

        def stack(item_id):
            return b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=1 if item_id else 0)

        def held(runtime, item_id, slot):
            return b.MobEquipmentPacket(runtime_id=runtime, item=stack(item_id), slot=slot, selected_slot=slot,
                                        container_id=b.ContainerID.INVENTORY)

        # BDS 1.26.66.1 offhand-wire-02, initial_owner_shield[67] and witness_updates_own_offhand[437].
        shield = RawPacket(b.MobEquipmentPacket.ID, bytes.fromhex(
            "016601010000000024ffff010a000003060044616d616765000000000000000000000000000000000000000000000077"
        ))
        local_arrows = RawPacket(b.InventoryContentPacket.ID, bytes.fromhex(
            "77012f010500000108000a0000000000000000000000000000000000000000"
        ))
        deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        deliver(b.AddActorPacket(runtime_id=8, entity_id=208, actor_type="minecraft:horse"),
                held(8, 0, 0),
                b.MobArmorEquipmentPacket(runtime_id=8, body=stack(19)))
        deliver(b.StartGamePacket(entity_id=107, runtime_id=7), held(7, 2, 1), held(1, 19, 0),
                b.AddPlayerPacket(uuid=UUID(int=8), runtime_id=1, name="Remote8", carried_item=stack(3)),
                b.MobArmorEquipmentPacket(runtime_id=1, head=stack(4), torso=stack(0), legs=stack(0), feet=stack(0)),
                held(1, 5, 2), shield, local_arrows,
                b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD,
                                                                        uuid=UUID(int=8), id=108, name="Remote8")]),
                b.PlayerHotbarPacket(container_id=b.ContainerID.INVENTORY, selected_slot=3, should_select_slot=True),
                held(7, 2, 1),
                b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await bot.join_nethernet("http://127.0.0.1:19142")
        self.assertEqual(bot.held_item.slot, 1)
        self.assertEqual((bot.offhand_item.id, bot.offhand_item.stack_size), (303, 5))
        remote = bot.observed_player_equipment(1)
        self.assertIsNotNone(remote.head, "the login stream must preserve remote MobArmorEquipment")
        self.assertEqual((remote.entity_id, remote.selected_slot, remote.main_hand.item_id, remote.head.item_id),
                         (108, 2, 5, 4))
        self.assertIsNone(bot.observed_player_equipment(7))
        equipment = bot.observed_actor_equipment(8)
        self.assertEqual((equipment.main_hand.item_id, equipment.body.item_id), (0, 19))
        self.assertIsNone(bot.observed_actor_equipment(1))
        actor = next(state for state in bot.observed_entities if state.runtime_id == 1)
        self.assertEqual(actor.last_observed_offhand, HeldItemStack(
            358, 1, 0, None, 0,
            bytes.fromhex("ffff010a000003060044616d616765000000000000000000000000000000000000000000"),
        ))

        unsubscribe = bot.subscribe_player_equipment_changes(snapshots.append, runtime_id=1)
        self.assertEqual(snapshots, [])
        pending = asyncio.create_task(bot.wait_for_player_equipment_change(1, timeout=1))
        await asyncio.sleep(0)
        deliver(held(1, 6, 4))
        changed = await pending
        self.assertEqual((changed.selected_slot, changed.main_hand.item_id), (4, 6))
        self.assertEqual(snapshots, [changed])
        unsubscribe()

        actor_snapshots = []
        unsubscribe = bot.subscribe_actor_equipment_changes(actor_snapshots.append, runtime_id=8)
        self.assertEqual(actor_snapshots, [])
        actor_pending = asyncio.create_task(bot.wait_for_actor_equipment_change(8, timeout=1))
        await asyncio.sleep(0)
        deliver(b.MobArmorEquipmentPacket(runtime_id=8, body=stack(19)),
                b.MobArmorEquipmentPacket(runtime_id=8, body=stack(20)))
        actor_changed = await actor_pending
        self.assertEqual(actor_changed.body.item_id, 20)
        self.assertEqual(actor_snapshots, [actor_changed])
        unsubscribe()
