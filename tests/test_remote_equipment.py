import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
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
        with self.assertRaises(FrozenInstanceError):
            snapshot.selected_slot = 1
        with self.assertRaises(FrozenInstanceError):
            snapshot.main_hand.count = 2

    async def test_atomic_four_slot_armour_and_actual_empty_slots(self):
        await self.deliver(self.player())
        unknown = self.entities.player_equipment(8)
        pending = await self.wait()
        await self.deliver(self.armor(head=10, chest=11, legs=12), self.armor(head=15))
        first = await pending
        self.assertIsNone(unknown.head)
        self.assertEqual((first.head.item_id, first.chest.item_id, first.legs.item_id, first.feet.item_id), (10, 11, 12, 0))
        self.assertEqual(first.feet.count, 0)
        newest = self.entities.player_equipment(8)
        self.assertEqual((newest.head.item_id, newest.chest.item_id, newest.legs.item_id, newest.feet.item_id), (15, 0, 0, 0))
        self.assertEqual(first.main_hand, newest.main_hand)

    async def test_repeat_same_slot_and_descriptor_ignored_but_distinct_slot_and_empty_are_changes(self):
        await self.deliver(self.player(), self.held(3, 1), self.armor(head=6))
        pending = await self.wait()
        await self.deliver(self.held(3, 1), self.held(10, 2, container=self.b.ContainerID.OFFHAND),
                           self.armor(head=6, body=11))
        self.assertFalse(pending.done())
        self.assertEqual(self.entities.get(8).last_observed_offhand.item_id, 10)
        equipment = self.entities.player_equipment(8)
        self.assertEqual((equipment.selected_slot, equipment.main_hand.item_id), (1, 3))
        await self.deliver(self.held(3, 2), self.held(0, 3))
        moved = await pending
        self.assertEqual((moved.selected_slot, moved.main_hand.item_id), (2, 3))
        now = self.entities.player_equipment(8)
        self.assertEqual((now.selected_slot, now.main_hand.item_id, now.main_hand.count), (3, 0, 0))

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
        pending = await self.wait()
        await self.deliver(self.b.RemoveActorPacket(entity_id=909), self.listed())
        self.assertIs(instance, self.entities._actor_instance(8))
        self.assertFalse(pending.done())
        self.assertEqual(self.entities.player_equipment(8).head.item_id, 6)
        self.assertEqual(self.entities.get(8).last_observed_offhand.item_id, 10)
        await self.deliver(self.held(12, 1))
        changed = await pending
        self.assertEqual((changed.entity_id, changed.main_hand.item_id, changed.head.item_id), (108, 12, 6))

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
        replacement = await self.wait()
        await self.deliver(self.held(12, 0, container=offhand), self.player(uuid=10, unique=110, carried=16),
                           self.armor(head=7))
        with self.assertRaises(LookupError):
            await replacement
        self.assertEqual((self.entities.player_equipment(8).name, self.entities.player_equipment(8).head.item_id),
                         ("Remote10", 7))
        self.assertIsNone(self.entities.get(8).last_observed_offhand)

    async def test_dimension_departure_and_return_preserve_roster_only(self):
        await self.deliver(self.listed(), self.player(), self.armor(head=6))
        waiting = await self.wait()
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.held(9, 1))
        self.assertEqual(self.entities.player_list()[0].entity_id, 108)
        self.assertIsNone(self.entities.player_equipment(8))
        with self.assertRaises(LookupError):
            await waiting
        await self.deliver(self.player(carried=8), self.armor(head=9))
        now = self.entities.player_equipment(8)
        self.assertEqual((now.main_hand.item_id, now.head.item_id), (8, 9))

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
        pending = await self.wait()
        await self.deliver(self.b.DisconnectPacket(), self.armor(head=11))
        with self.assertRaises(ConnectionError):
            await pending
        self.assertIsNone(self.entities.player_equipment(8))


class RemoteEquipmentStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_packets_preserve_wire_order_and_own_hotbar(self):
        bot = Bot("GearObserver", protocol=2225)
        self.addAsyncCleanup(bot.close)
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
        actor = next(state for state in bot.observed_entities if state.runtime_id == 1)
        self.assertEqual(actor.last_observed_offhand, HeldItemStack(
            358, 1, 0, None, 0,
            bytes.fromhex("ffff010a000003060044616d616765000000000000000000000000000000000000000000"),
        ))
