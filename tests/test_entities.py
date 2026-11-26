import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
from uuid import UUID

from endpilot.compression import PacketConnection
from endpilot.entities import Entities, EntityMovement, EntitySpawn, EntityState, ItemPickup
from endpilot.inventory import Inventory
from endpilot.movement import Movement
from endpilot.protocol import Protocol, RawPacket, write_batch
from test_scheduled_input import Wire


def _movement_at(packets, position):
    movement = Movement(packets, 7)
    b = packets.protocol.binding
    x, y, z = position
    movement._on_packet(packets.protocol.encode(b.MovePlayerPacket(player_id=7, pos=b.Vec3(x=x, y=y, z=z))))
    return movement


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_attack_uses_observed_actor_and_selected_item_in_typed_packet(self):
        protocol = Protocol(2193)
        b = protocol.binding
        item = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=1, aux_value=0, net_id_variant=9,
            block_runtime_id=123, user_data_buffer=b"\x00" * 10,
        )
        callbacks = []
        packets = SimpleNamespace(
            protocol=protocol, send=AsyncMock(),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        entities = Entities(packets)
        entities._record(EntityState(42, 77, "minecraft:cow", (2.5, 100.0, 0.5)))
        movement = _movement_at(packets, (0.5, 101.62, 0.5))
        inventory = Inventory(packets, 7)
        inventory._set_slot(2, item)
        await inventory.select_hotbar(2)
        packets.send.reset_mock()

        await entities.attack(movement, inventory, 77, hit_height=0.75)
        packets.send.assert_awaited_once()
        packet = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        self.assertIsInstance(packet, b.InventoryTransactionPacket)
        tx = packet.variant_transaction
        self.assertIsInstance(tx, b.ItemUseOnActorInventoryTransaction)
        self.assertEqual(int(tx.runtime_id), 77)
        self.assertEqual(tx.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.ATTACK)
        self.assertEqual(tx.slot, 2)
        self.assertEqual(int(tx.item.id), 1)
        self.assertEqual(int(tx.item.net_id_variant), 9)
        self.assertAlmostEqual(tx.from_pos.y, 101.62, places=2)
        self.assertAlmostEqual(tx.hit_pos.x, 2.5)
        self.assertAlmostEqual(tx.hit_pos.y, 100.75)
        self.assertEqual(tx.transaction.actions, [])
        inventory.close()
        self.assertEqual(callbacks, [])

    async def test_swing_sends_client_animate_packet_without_actor_transaction(self):
        protocol = Protocol(2193)
        b = protocol.binding
        packets = SimpleNamespace(protocol=protocol, send=AsyncMock())
        await Entities(packets).swing(17)
        packets.send.assert_awaited_once()
        packet = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        self.assertIsInstance(packet, b.AnimatePacket)
        self.assertEqual(packet.action, b.AnimatePacket.Action.SWING)
        self.assertEqual(int(packet.runtime_id), 17)
        self.assertEqual(packet.data, 0.0)
        self.assertEqual(packet.swing_source, b.ActorSwingSource.NONE)

    async def test_attack_unobserved_actor_uses_upstream_player_position_fallback(self):
        protocol = Protocol(2193)
        packets = SimpleNamespace(protocol=protocol, send=AsyncMock())
        entities = Entities(packets)
        movement = _movement_at(packets, (3.5, 101.62, -0.5))
        item = protocol.binding.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
        inventory = SimpleNamespace(item_or_empty=lambda slot: item, selected_slot=0)
        await entities.attack(movement, inventory, 999)
        packet = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        tx = packet.variant_transaction
        self.assertEqual(int(tx.runtime_id), 999)
        self.assertAlmostEqual(tx.hit_pos.x, 3.5)
        self.assertAlmostEqual(tx.hit_pos.y, 102.12, places=2)
        self.assertAlmostEqual(tx.hit_pos.z, -0.5)

    async def test_interact_uses_selected_item_and_observed_actor_in_typed_packet(self):
        protocol = Protocol(2193)
        b = protocol.binding
        packets = SimpleNamespace(protocol=protocol, send=AsyncMock())
        entities = Entities(packets)
        entities._record(EntityState(42, 77, "minecraft:cow", (2.5, 100.0, 0.5)))
        movement = _movement_at(packets, (0.5, 101.62, 0.5))
        item = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=2, aux_value=0, net_id_variant=17,
            block_runtime_id=0, user_data_buffer=b"\x00" * 10,
        )
        inventory = SimpleNamespace(selected_slot=3, item_or_empty=lambda slot: item)
        await entities.interact(movement, inventory, 77, hit_height=0.75)
        packets.send.assert_awaited_once()
        packet = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        self.assertIsInstance(packet, b.InventoryTransactionPacket)
        tx = packet.variant_transaction
        self.assertIsInstance(tx, b.ItemUseOnActorInventoryTransaction)
        self.assertEqual(tx.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.INTERACT)
        self.assertEqual(int(tx.runtime_id), 77)
        self.assertEqual(tx.slot, 3)
        self.assertEqual(int(tx.item.id), 1)
        self.assertEqual(int(tx.item.net_id_variant), 17)
        self.assertEqual(tx.transaction.actions, [])
        self.assertAlmostEqual(tx.from_pos.y, 101.62, places=2)
        self.assertAlmostEqual(tx.hit_pos.x, 2.5)
        self.assertAlmostEqual(tx.hit_pos.y, 100.75)

    async def test_spawn_filters_type_and_returns_server_ids_and_position(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.AddActorPacket.ID, b"other")
        cow = RawPacket(binding.AddActorPacket.ID, b"cow")
        decoded = {
            b"other": SimpleNamespace(actor_type="minecraft:pig"),
            b"cow": SimpleNamespace(
                actor_type="minecraft:cow", entity_id=42, runtime_id=77,
                pos=SimpleNamespace(x=2.5, y=101.0, z=0.5),
            ),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.AddActorPacket.ID)
            self.assertEqual(timeout, 5)
            self.assertFalse(match(other))
            self.assertTrue(match(cow))
            return cow

        packets.wait_for_packet.side_effect = wait
        entity = await Entities(packets).wait_for_spawn(actor_type="minecraft:cow", timeout=5)
        self.assertEqual(entity, EntitySpawn(42, 77, "minecraft:cow", (2.5, 101.0, 0.5)))

    async def test_removal_matches_unique_id_not_runtime_id(self):
        binding = Protocol(2193).binding
        packet = binding.RemoveActorPacket(entity_id=42)
        protocol = Protocol(2193)
        raw = protocol.encode(packet)
        self.assertEqual(protocol.decode(raw).entity_id, 42)
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.RemoveActorPacket.ID)
            self.assertFalse(match(protocol.encode(binding.RemoveActorPacket(entity_id=77))))
            self.assertTrue(match(raw))
            return raw

        packets.wait_for_packet.side_effect = wait
        self.assertIsNone(await Entities(packets).wait_for_removal(42))

    async def test_movement_matches_runtime_id_and_preserves_omitted_axes(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.MoveActorDeltaPacket.ID, b"other")
        moved = RawPacket(binding.MoveActorDeltaPacket.ID, b"moved")
        decoded = {
            b"other": SimpleNamespace(move_data=SimpleNamespace(runtime_id=999)),
            b"moved": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, new_position_x=6.0,
                new_position_y=None, new_position_z=None,
            )),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_ids, *, match, timeout):
            self.assertEqual(set(packet_ids), {
                binding.MoveActorAbsolutePacket.ID, binding.MoveActorDeltaPacket.ID,
            binding.MovePlayerPacket.ID,
            })
            self.assertFalse(match(other))
            self.assertTrue(match(moved))
            self.assertEqual(timeout, 8)
            return moved

        packets.wait_for_packet.side_effect = wait
        result = await Entities(packets).wait_for_movement(77, timeout=8)
        self.assertEqual(result, EntityMovement(77, 6.0, None, None))

    async def test_absolute_actor_movement_has_all_coordinates(self):
        binding = Protocol(2193).binding
        packet = RawPacket(binding.MoveActorAbsolutePacket.ID, b"absolute")
        move_data = SimpleNamespace(
            runtime_id=77, pos=SimpleNamespace(x=6.0, y=100.0, z=0.5),
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda raw: SimpleNamespace(move_data=move_data)),
            wait_for_packet=AsyncMock(return_value=packet),
        )
        result = await Entities(packets).wait_for_movement(77)
        self.assertEqual(result, EntityMovement(77, 6.0, 100.0, 0.5))

    async def test_snapshot_accumulates_partial_moves_and_removal(self):
        binding = Protocol(2193).binding
        decoded = {
            b"spawn": SimpleNamespace(
                entity_id=42, runtime_id=77, actor_type="minecraft:cow",
                pos=SimpleNamespace(x=2.5, y=100.0, z=0.5), links=[],
            ),
            b"delta": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, new_position_x=6.0,
                new_position_y=None, new_position_z=None,
            )),
            b"absolute": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, pos=SimpleNamespace(x=7.0, y=101.0, z=1.0),
            )),
            b"remove": SimpleNamespace(entity_id=42),
        }
        callbacks = []

        def subscribe(callback):
            callbacks.append(callback)
            return lambda: callbacks.remove(callback)

        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=binding, decode=lambda raw: decoded[raw.body],
            ),
            subscribe_raw=subscribe,
        )
        entities = Entities(packets)
        entities.start()

        def emit(packet_type, body):
            callbacks[0](RawPacket(packet_type, body))

        emit(binding.MoveActorDeltaPacket.ID, b"delta")
        self.assertEqual(entities.snapshot(), ())
        emit(binding.AddActorPacket.ID, b"spawn")
        initial = entities.snapshot()
        self.assertEqual(initial, (
            EntityState(42, 77, "minecraft:cow", (2.5, 100.0, 0.5)),
        ))
        emit(binding.MoveActorDeltaPacket.ID, b"delta")
        self.assertEqual(entities.snapshot()[0].position, (6.0, 100.0, 0.5))
        self.assertEqual(initial[0].position, (2.5, 100.0, 0.5))
        emit(binding.MoveActorAbsolutePacket.ID, b"absolute")
        self.assertEqual(entities.snapshot()[0].position, (7.0, 101.0, 1.0))
        emit(binding.RemoveActorPacket.ID, b"remove")
        self.assertEqual(entities.snapshot(), ())
        emit(binding.AddActorPacket.ID, b"spawn")
        self.assertEqual(len(entities.snapshot()), 1)
        emit(binding.ChangeDimensionPacket.ID, b"dimension")
        self.assertEqual(entities.snapshot(), ())
        entities.close()
        self.assertEqual(callbacks, [])


    async def test_item_drop_from_registry_tracks_movement_and_take(self):
        b = Protocol(2193).binding
        callbacks = []
        decoded = {
            b"registry": SimpleNamespace(items=[
                SimpleNamespace(id=42, name="minecraft:beef"),
            ]),
            b"drop": SimpleNamespace(
                id=101, runtime_id=202,
                item=SimpleNamespace(id=42, stack_size=2),
                pos=SimpleNamespace(x=3.5, y=100.5, z=0.5),
            ),
            b"move": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=202, new_position_x=4.5,
                new_position_y=None, new_position_z=None,
            )),
            b"take": SimpleNamespace(item_id=202, actor_id=7),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: callbacks.append(callback) or (lambda: callbacks.remove(callback)),
        )
        entities = Entities(packets)
        entities.start()
        callbacks[0](RawPacket(b.ItemRegistryPacket.ID, b"registry"))
        callbacks[0](RawPacket(b.AddItemActorPacket.ID, b"drop"))
        initial = entities.drops()[0]
        self.assertEqual(initial, EntityState(
            101, 202, "minecraft:item", (3.5, 100.5, 0.5), "minecraft:beef", 2, 42
        ))
        callbacks[0](RawPacket(b.MoveActorDeltaPacket.ID, b"move"))
        self.assertEqual(entities.drops()[0].position, (4.5, 100.5, 0.5))
        self.assertEqual(initial.position, (3.5, 100.5, 0.5))
        callbacks[0](RawPacket(b.TakeItemActorPacket.ID, b"take"))
        self.assertEqual(entities.drops(), ())
        self.assertEqual(entities.snapshot(), ())
        entities.close()
        self.assertEqual(callbacks, [])

    async def test_item_drop_fallback_id_and_unique_id_removal(self):
        b = Protocol(2193).binding
        callbacks = []
        decoded = {
            b"drop": SimpleNamespace(
                id=101, runtime_id=202,
                item=SimpleNamespace(id=52, stack_size=1),
                pos=SimpleNamespace(x=0.5, y=101, z=0.5),
            ),
            b"remove": SimpleNamespace(entity_id=101),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: callbacks.append(callback) or (lambda: callbacks.remove(callback)),
        )
        entities = Entities(packets)
        entities.start()
        callbacks[0](RawPacket(b.AddItemActorPacket.ID, b"drop"))
        self.assertEqual(entities.drops()[0].item_id, "#52")
        callbacks[0](RawPacket(b.RemoveActorPacket.ID, b"remove"))
        self.assertEqual(entities.drops(), ())
        entities.close()

    def item_drop_stream(self):
        protocol = Protocol(2225)
        b = protocol.binding
        wire = Wire(protocol)
        packets = PacketConnection(wire, protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        entities = Entities(packets)
        entities.start()
        packets.start_receiving()
        self.addAsyncCleanup(packets.close)
        self.addCleanup(entities.close)

        def deliver(*packets):
            wire.incoming.put_nowait(write_batch(map(protocol.encode, packets)))

        return b, entities, deliver

    async def test_wait_for_item_drop_captures_name_before_later_batch_updates(self):
        b, entities, deliver = self.item_drop_stream()
        item = b.SerializedNetworkItemStackDescriptor(id=4, stack_size=3)
        waiting = asyncio.create_task(entities.wait_for_item_drop(timeout=1))
        await asyncio.sleep(0)
        deliver(
            b.ItemRegistryPacket(items=[b.ItemData(id=4, name="custom:early")]),
            b.AddItemActorPacket(id=30, runtime_id=31, item=item, pos=b.Vec3(x=1, y=2, z=3)),
            b.ItemRegistryPacket(items=[b.ItemData(id=4, name="custom:late")]),
            b.AddItemActorPacket(id=32, runtime_id=33, item=item),
            b.DisconnectPacket(),
            b.ItemRegistryPacket(items=[b.ItemData(id=4, name="custom:trailing")]),
            b.AddItemActorPacket(id=34, runtime_id=35, item=item),
        )
        result = await waiting
        self.assertEqual(result, EntityState(
            30, 31, "minecraft:item", (1.0, 2.0, 3.0), "custom:early", 3, 4
        ))
        self.assertEqual([(drop.runtime_id, drop.item_id) for drop in entities.drops()],
                         [(31, "custom:early"), (33, "custom:late")])

    async def test_wait_for_item_drop_ends_on_disconnect_before_late_drop(self):
        b, entities, deliver = self.item_drop_stream()
        waiting = asyncio.create_task(entities.wait_for_item_drop(timeout=1))
        await asyncio.sleep(0)
        deliver(
            b.DisconnectPacket(),
            b.AddItemActorPacket(id=30, runtime_id=31, item=b.SerializedNetworkItemStackDescriptor(id=4, stack_size=1)),
        )
        with self.assertRaises(ConnectionError):
            await waiting
        self.assertEqual(entities.drops(), ())
        with self.assertRaises(ConnectionError):
            await entities.wait_for_item_drop(timeout=0)

    async def test_collect_drop_releases_movement_after_confirmed_take(self):
        protocol = Protocol(2193)
        b = protocol.binding
        packet_ready = asyncio.Event()
        move_started = asyncio.Event()
        incoming = asyncio.get_running_loop().create_future()

        async def wait_for_packet(packet_id, *, match, timeout):
            self.assertEqual(packet_id, (b.TakeItemActorPacket.ID, b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID))
            self.assertEqual(timeout, 2)
            packet_ready.set()
            raw = await incoming
            entities._on_packet(raw)
            self.assertTrue(match(raw))
            return raw

        async def follow(*_args, **_kwargs):
            move_started.set()
            await asyncio.Event().wait()

        packets = SimpleNamespace(
            protocol=protocol, wait_for_packet=wait_for_packet,
        )
        entities = Entities(packets)
        item = EntityState(13, 23, "minecraft:item", (3.5, 100.5, 0.5), "minecraft:dirt", 1, 3)
        entities._record(item)
        movement = SimpleNamespace(follow_entity=AsyncMock(side_effect=follow))
        task = asyncio.create_task(entities.collect_drop(movement, 23, 7, timeout=2))
        try:
            await asyncio.wait_for(packet_ready.wait(), 1)
            await asyncio.wait_for(move_started.wait(), 1)
            incoming.set_result(protocol.encode(
                b.TakeItemActorPacket(item_id=23, actor_id=7)
            ))
            self.assertEqual(await asyncio.wait_for(task, 1), item)
            movement.follow_entity.assert_awaited_once_with(
                entities, 23, ticks=120, stop_distance=0.0, _resolve=ANY,
            )
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_collect_drop_rejects_unknown_actor_without_walking(self):
        movement = SimpleNamespace(follow_entity=AsyncMock())
        entities = Entities(SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "item actor"):
            await entities.collect_drop(movement, 13, 7)
        movement.follow_entity.assert_not_awaited()

    async def test_other_player_spawn_move_and_leave_resolves_unique_id_from_list(self):
        binding = Protocol(2193).binding
        callbacks = []
        uuid = UUID("b8d5770e-ae7a-4f40-9934-67ec14a33f20")
        decoded = {
            b"list_add": binding.PlayerListPacket(entries=[binding.PlayerListPacket.AddEntry(
                action=binding.PlayerListPacketType.ADD, uuid=uuid, id=900, name="EndPilotTest",
            )]),
            b"spawn": SimpleNamespace(
                uuid=uuid, name="EndPilotTest", runtime_id=200,
                pos=SimpleNamespace(x=2.5, y=101.62, z=0.5), links=[],
            ),
            b"local": SimpleNamespace(
                uuid="local", name="EndPilotObserver", runtime_id=100,
                pos=SimpleNamespace(x=0.5, y=101.62, z=0.5), links=[],
            ),
            b"move": SimpleNamespace(
                player_id=200, pos=SimpleNamespace(x=2.5, y=101.62, z=4.5),
                riding_id=0, reset_position=0, on_ground=True,
            ),
            b"remove": SimpleNamespace(entity_id=900),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        entities = Entities(packets, own_runtime_id=100)
        entities.start()
        try:
            emit = lambda packet_id, body: callbacks[0](RawPacket(packet_id, body))
            emit(binding.PlayerListPacket.ID, b"list_add")
            emit(binding.AddPlayerPacket.ID, b"local")
            emit(binding.AddPlayerPacket.ID, b"spawn")
            state, = entities.snapshot()
            self.assertEqual((state.entity_id, state.runtime_id), (900, 200))
            self.assertEqual((state.actor_type, state.name), ("minecraft:player", "EndPilotTest"))
            self.assertEqual(state.position, (2.5, 101.62, 0.5))
            emit(binding.MovePlayerPacket.ID, b"move")
            self.assertEqual(entities.snapshot()[0].position, (2.5, 101.62, 4.5))
            self.assertEqual(state.position, (2.5, 101.62, 0.5))
            emit(binding.RemoveActorPacket.ID, b"remove")
            self.assertEqual(entities.snapshot(), ())
        finally:
            entities.close()
        self.assertEqual(callbacks, [])

    async def test_player_list_after_spawn_and_remove_by_uuid_keeps_other_players(self):
        binding = Protocol(2193).binding
        alice_uuid, bob_uuid = UUID(int=1), UUID(int=2)
        decoded = {
            b"a": SimpleNamespace(
                uuid=alice_uuid, name="Alice", runtime_id=10,
                pos=SimpleNamespace(x=0, y=101, z=0), links=[],
            ),
            b"b": SimpleNamespace(
                uuid=bob_uuid, name="Bob", runtime_id=11,
                pos=SimpleNamespace(x=1, y=101, z=0), links=[],
            ),
            b"list": binding.PlayerListPacket(entries=[
                binding.PlayerListPacket.AddEntry(
                    action=binding.PlayerListPacketType.ADD, uuid=alice_uuid, id=50, name="Alice",
                ),
                binding.PlayerListPacket.AddEntry(
                    action=binding.PlayerListPacketType.ADD, uuid=bob_uuid, id=51, name="Bob",
                ),
            ]),
            b"left": binding.PlayerListPacket(entries=[binding.PlayerListPacket.RemoveEntry(
                action=binding.PlayerListPacketType.REMOVE, uuid=alice_uuid,
            )]),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body]),
        )
        entities = Entities(packets)
        entities._on_packet(RawPacket(binding.AddPlayerPacket.ID, b"a"))
        entities._on_packet(RawPacket(binding.AddPlayerPacket.ID, b"b"))
        self.assertEqual(len(entities.snapshot()), 2)  # Unknown unique IDs must not collide.
        entities._on_packet(RawPacket(binding.PlayerListPacket.ID, b"list"))
        self.assertEqual({actor.entity_id for actor in entities.snapshot()}, {50, 51})
        entities._on_packet(RawPacket(binding.PlayerListPacket.ID, b"left"))
        remaining, = entities.snapshot()
        self.assertEqual((remaining.entity_id, remaining.name), (51, "Bob"))

        # Dimension changes discard runtime actors, not server player-list identities.
        entities._on_packet(RawPacket(binding.ChangeDimensionPacket.ID, b"dimension"))
        self.assertEqual(entities.snapshot(), ())
        decoded[b"b-again"] = SimpleNamespace(
            uuid=bob_uuid, name="Bob", runtime_id=12,
            pos=SimpleNamespace(x=2, y=101, z=0), links=[],
        )
        decoded[b"remove"] = SimpleNamespace(entity_id=51)
        entities._on_packet(RawPacket(binding.AddPlayerPacket.ID, b"b-again"))
        rejoined, = entities.snapshot()
        self.assertEqual((rejoined.entity_id, rejoined.runtime_id), (51, 12))
        entities._on_packet(RawPacket(binding.RemoveActorPacket.ID, b"remove"))
        self.assertEqual(entities.snapshot(), ())

    async def test_player_movement_wait_matches_runtime_and_full_position(self):
        binding = Protocol(2193).binding
        raw = RawPacket(binding.MovePlayerPacket.ID, b"player")
        other = RawPacket(binding.MovePlayerPacket.ID, b"other")
        decoded = {
            b"player": SimpleNamespace(
                player_id=200, pos=SimpleNamespace(x=5, y=101.5, z=2),
            ),
            b"other": SimpleNamespace(
                player_id=201, pos=SimpleNamespace(x=0, y=0, z=0),
            ),
        }
        async def wait(ids, *, match, timeout):
            self.assertIn(binding.MovePlayerPacket.ID, ids)
            self.assertFalse(match(other))
            self.assertTrue(match(raw))
            self.assertEqual(timeout, 9)
            return raw

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda packet: decoded[packet.body]),
            wait_for_packet=wait,
        )
        result = await Entities(packets).wait_for_movement(200, timeout=9)
        self.assertEqual(result, EntityMovement(200, 5.0, 101.5, 2.0))


class ItemPickupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = b = self.protocol.binding
        self.wire = Wire(self.protocol)
        self.packets = PacketConnection(self.wire, self.protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.entities = Entities(self.packets, item_names={5: "minecraft:diamond"},
                                 own_runtime_id=7, own_entity_id=1007)
        self.entities.start()
        self.packets.start_receiving()
        self.addAsyncCleanup(self.packets.close)
        self.addCleanup(self.entities.close)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        processed = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (*packets, marker))))
        await processed

    async def wait_for_pickup(self, **filters):
        waiter = asyncio.create_task(self.entities.wait_for_item_pickup(**filters))
        self.addAsyncCleanup(self.cancel_waiter, waiter)
        await asyncio.sleep(0)
        return waiter

    async def cancel_waiter(self, waiter):
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

    def item(self, entity_id, runtime_id, count=2):
        b = self.b
        return b.AddItemActorPacket(id=entity_id, runtime_id=runtime_id, pos=b.Vec3(x=2, y=3, z=4),
                                    item=b.SerializedNetworkItemStackDescriptor(id=5, stack_size=count))

    def player(self, entity_id, runtime_id, uuid):
        b = self.b
        return (
            b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
                action=b.PlayerListPacketType.ADD, uuid=uuid, id=entity_id, name="Collector",
            )]),
            b.AddPlayerPacket(uuid=uuid, runtime_id=runtime_id, name="Collector", pos=b.Vec3()),
        )

    def take(self, item, collector):
        return self.b.TakeItemActorPacket(item_id=item, actor_id=collector)

    async def test_pickup_has_wire_order_snapshot_and_matches_unique_filters(self):
        b = self.b
        remote = UUID(int=1)
        await self.deliver(self.item(101, 21), *self.player(102, 22, remote))
        observed = self.entities.get(21)
        waiting = await self.wait_for_pickup(item_entity_id=101, collector_entity_id=102)
        await self.deliver(self.take(99, 22), self.take(21, 22), b.RemoveActorPacket(entity_id=101),
                           self.item(103, 21, count=5), b.RemoveActorPacket(entity_id=102))

        pickup = await asyncio.wait_for(waiting, 1)
        self.assertEqual(pickup, ItemPickup(21, 22, 101, 102, observed))
        self.assertEqual(pickup.item.item_count, 2)
        self.assertEqual(self.entities.get(21).item_count, 5)
        with self.assertRaises(FrozenInstanceError):
            pickup.collector_entity_id = 103

    async def test_pickup_preserves_unobserved_ids_and_matches_runtime_filters(self):
        await self.deliver(self.take(21, 22))
        waiting = await self.wait_for_pickup(item_runtime_id=21, collector_runtime_id=22)
        await self.deliver(self.take(99, 22), self.take(21, 98))
        self.assertFalse(waiting.done())
        await self.deliver(self.take(21, 22))
        self.assertEqual(await asyncio.wait_for(waiting, 1), ItemPickup(21, 22, None, None, None))
        own = await self.wait_for_pickup()
        await self.deliver(self.take(71, 7))
        self.assertEqual(await asyncio.wait_for(own, 1), ItemPickup(71, 7, None, 1007, None))

    async def test_removing_an_item_does_not_confirm_pickup(self):
        await self.deliver(self.item(101, 21))
        waiting = await self.wait_for_pickup(item_runtime_id=21, timeout=0.1)
        await self.deliver(self.b.RemoveActorPacket(entity_id=101))
        self.assertIsNone(self.entities.get(21))
        with self.assertRaises(TimeoutError):
            await waiting

    async def test_known_runtime_filters_reject_reused_item_and_collector_ids(self):
        b = self.b
        for replacement in ("item", "collector"):
            with self.subTest(replacement=replacement):
                remote = UUID(int=1)
                await self.deliver(self.item(101, 21), *self.player(102, 22, remote))
                old = await self.wait_for_pickup(item_runtime_id=21, collector_runtime_id=22)
                if replacement == "item":
                    await self.deliver(b.RemoveActorPacket(entity_id=101), self.item(103, 21, count=4))
                    item_id, collector_id = 103, 102
                else:
                    await self.deliver(*self.player(104, 22, UUID(int=2)), b.PlayerListPacket(entries=[
                        b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=remote),
                    ]))
                    item_id, collector_id = 101, 104

                current = await self.wait_for_pickup(item_entity_id=item_id, collector_entity_id=collector_id)
                await self.deliver(self.take(21, 22))
                pickup = await asyncio.wait_for(current, 1)
                self.assertEqual((pickup.item_entity_id, pickup.collector_entity_id), (item_id, collector_id))
                self.assertFalse(old.done())
                await self.cancel_waiter(old)

    async def test_old_player_list_remove_preserves_the_drop_that_reused_its_runtime_id(self):
        b = self.b
        old = UUID(int=1)
        other = UUID(int=2)
        await self.deliver(*self.player(102, 22, old), *self.player(104, 24, other))
        await self.deliver(self.item(103, 22, count=4))
        self.assertEqual({player.uuid for player in self.entities.player_list()}, {old, other})
        waiting = await self.wait_for_pickup(item_entity_id=103)

        await self.deliver(b.PlayerListPacket(entries=[
            b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=old),
        ]))
        self.assertEqual([player.uuid for player in self.entities.player_list()], [other])
        self.assertEqual(self.entities.get(24).entity_id, 104)
        await self.deliver(self.take(22, 7))

        pickup = await asyncio.wait_for(waiting, 1)
        self.assertEqual(pickup, ItemPickup(22, 7, 103, 1007, EntityState(
            103, 22, "minecraft:item", (2.0, 3.0, 4.0), "minecraft:diamond", 4, 5,
        )))
        await self.deliver(b.PlayerListPacket(entries=[
            b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=other),
        ]))
        self.assertIsNone(self.entities.get(24))
        self.assertEqual(self.entities.player_list(), ())

    async def test_dimension_change_stops_wait_before_reused_ids_are_observed(self):
        b = self.b
        await self.deliver(self.item(101, 21))
        waiting = await self.wait_for_pickup(item_runtime_id=21)
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), self.item(103, 21), self.take(21, 22))
        with self.assertRaisesRegex(RuntimeError, "dimension changed"):
            await waiting
        current = await self.wait_for_pickup()
        await self.deliver(self.take(21, 22))
        self.assertEqual(await asyncio.wait_for(current, 1), ItemPickup(21, 22, None, None, None))

    async def test_disconnect_stops_wait_and_ignores_later_pickup(self):
        waiting = await self.wait_for_pickup()
        await self.deliver(self.b.DisconnectPacket(), self.take(21, 22))
        with self.assertRaises(ConnectionError):
            await waiting
        with self.assertRaises(ConnectionError):
            await self.entities.wait_for_item_pickup()


class RemotePlayerViewTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.entities = Entities(SimpleNamespace(protocol=self.protocol), own_runtime_id=7, own_entity_id=107)

    def deliver(self, packet):
        self.entities._on_packet(self.protocol.encode(packet))

    def metadata(self, words):
        b = self.b
        return b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=number, payload=b.DataItemInt64Payload(value=value)) for number, value in words.items()
        ])

    def spawn(self, *, runtime=8, unique=108, words=None):
        b = self.b
        return b.AddPlayerPacket(
            uuid=UUID(int=unique), runtime_id=runtime, name="Observed", pos=b.Vec3(x=3.5, y=100, z=4.5),
            rot=b.Vec2(x=-8, y=-90), y_head_rot=-90, unpack=self.metadata(words if words is not None else {0: 0, 92: 0}),
            abilities_data=b.SerializedAbilitiesData(target_player=unique),
        )

    def update(self, words, runtime=8):
        self.deliver(self.b.SetActorDataPacket(id=runtime, packed_items=self.metadata(words)))

    def move(self, pitch, yaw, head_yaw, *, grounded=True, mode=0, riding_id=0, runtime=8):
        b = self.b
        self.deliver(b.MovePlayerPacket(
            player_id=runtime, pos=b.Vec3(x=4.5, y=101.62, z=4.5), rot=b.Vec2(x=pitch, y=yaw),
            y_head_rot=head_yaw, on_ground=grounded, reset_position=mode, riding_id=riding_id,
        ))

    def assert_pose(self, eye, rotation, *, runtime=8):
        actual_eye, actual_rotation = self.entities.player_view_pose(runtime)
        self.assertEqual(actual_rotation, rotation)
        for expected, actual in zip(eye, actual_eye, strict=True):
            self.assertAlmostEqual(actual, expected, places=4)

    def test_reads_initial_stance_from_an_anonymized_stock_add_player_body(self):
        # Protocol 2225 AddPlayer before PLAYER_SPAWN, with synthetic identity fields.
        fixture = Path(__file__).with_name("fixtures") / "remote-player-add-2225.hex"
        raw = RawPacket(self.b.AddPlayerPacket.ID, bytes.fromhex(fixture.read_text(encoding="ascii")))
        player = self.protocol.decode(raw)
        self.assertTrue(player.uuid == UUID("00000000-0000-4000-8000-000000000001"), "Expected the fixture UUID")
        self.assertTrue(player.name == "FixtureActor", "Expected the fixture player name")
        names = [entry.payload.value for entry in player.unpack.data if entry.id == self.b.ActorDataIDs.NAME]
        self.assertTrue(names == [player.name], "Expected NAME metadata to match the fixture player name")
        self.assertTrue(player.abilities_data.target_player == 0x1122334455667788, "Expected the fixture entity ID")
        self.assertTrue(player.device_id == "deadbeef" * 4, "Expected the fixture device ID")
        self.assertFalse(player.platform_online_id)
        self.assertEqual(self.protocol.encode(player), raw)
        self.entities._on_packet(raw)
        self.assert_pose((8.5, 221.52, 8.5), (-8, -90), runtime=1)
        self.assertEqual(self.entities.get(1).position, (8.5, 220, 8.5))

    def test_waits_for_both_metadata_words_and_keeps_the_unmentioned_word(self):
        b = self.b
        self.deliver(self.spawn(words={0: 0}))
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({92: 0}, runtime=9)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({92: 0})
        self.assert_pose((3.5, 101.52, 4.5), (-8, -90))
        self.update({0: 1 << int(b.ActorFlags.SNEAKING)})
        self.assert_pose((3.5, 101.17, 4.5), (-8, -90))
        self.update({92: 1 << (int(b.ActorFlags.CRAWLING) - 64)})
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({0: 0})
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({92: 0})
        self.assertIsNone(self.entities.player_view_pose(8))
        self.move(-8, -90, -90)
        self.assert_pose((4.5, 101.52, 4.5), (-8, -90))
        self.update({0: 1 << int(b.ActorFlags.SWIMMING)})
        self.assertIsNone(self.entities.player_view_pose(8))

    def test_preserves_original_movement_position_and_invalidates_partial_turns(self):
        b = self.b
        self.deliver(self.spawn())
        self.move(-8, -90, 180)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.move(-18, -180, 180)
        self.assert_pose((4.5, 101.52, 4.5), (-18, -180))
        self.assertAlmostEqual(self.entities.get(8).position[1], 101.62, places=4)
        self.update({0: 1 << int(b.ActorFlags.SNEAKING)})
        self.assert_pose((4.5, 101.17, 4.5), (-18, -180))
        self.move(12, 45, 45, mode=b.PlayerPositionModeComponent.PositionMode.ONLY_HEAD_ROT)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.move(12, 45, 45, grounded=False)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.move(12, 45, 45, riding_id=15)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.move(12, 45, 45)
        self.assert_pose((4.5, 101.17, 4.5), (12, 45))

    def test_clears_for_removal_same_runtime_spawn_dimension_and_close(self):
        b = self.b
        self.deliver(self.spawn())
        self.update({0: 1 << int(b.ActorFlags.SNEAKING)})
        self.deliver(b.RemoveActorPacket(entity_id=108))
        self.move(0, 0, 0)
        self.update({0: 0, 92: 0})
        self.assertIsNone(self.entities.player_view_pose(8))

        self.deliver(self.spawn(words={}))
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({0: 0, 92: 0})
        self.assert_pose((3.5, 101.52, 4.5), (-8, -90))
        self.deliver(self.spawn(unique=109, words={0: 0}))
        self.assertIsNone(self.entities.player_view_pose(8))
        self.update({92: 0})
        self.assert_pose((3.5, 101.52, 4.5), (-8, -90))
        self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.entities.player_view_pose(8))
        self.deliver(self.spawn())
        self.entities.close()
        self.assertIsNone(self.entities.player_view_pose(8))

    def test_late_identity_after_removal_cannot_reexpose_a_pose(self):
        b = self.b
        missing_identity = self.spawn(unique=108)
        missing_identity.abilities_data = b.SerializedAbilitiesData(target_player=0)
        self.deliver(missing_identity)
        self.assertIsNone(self.entities.player_view_pose(8))
        self.deliver(b.RemoveActorPacket(entity_id=108))
        self.deliver(b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=108), id=108, name="Observed",
        )]))
        self.move(0, 0, 0)
        self.update({0: 0, 92: 0})
        self.assertIsNone(self.entities.player_view_pose(8))
        self.deliver(self.spawn())
        self.assert_pose((3.5, 101.52, 4.5), (-8, -90))


class NearestPlayerTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.entities = Entities(SimpleNamespace(protocol=self.protocol), own_runtime_id=7, own_entity_id=1007)
        self.origin = (0.0, 100.0, 0.0)

    def deliver(self, *packets):
        for packet in packets:
            self.entities._on_packet(self.protocol.encode(packet))

    def listed(self, entity_id, name):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=entity_id), id=entity_id, name=name,
        )])

    def spawn(self, entity_id, runtime_id, name, feet):
        return self.b.AddPlayerPacket(uuid=UUID(int=entity_id), runtime_id=runtime_id, name=name,
                                      pos=self.b.Vec3(x=feet[0], y=feet[1], z=feet[2]))

    def move(self, runtime_id, eye):
        return self.b.MovePlayerPacket(player_id=runtime_id, pos=self.b.Vec3(x=eye[0], y=eye[1], z=eye[2]))

    def test_ranks_feet_in_three_dimensions_and_preserves_raw_position(self):
        self.deliver(self.listed(101, "East"), self.spawn(101, 11, "East", (2, 100, 0)),
                     self.listed(102, "Below"), self.spawn(102, 12, "Below", (0, 97, 0)))
        self.assertEqual(self.entities.nearest_player(self.origin).name, "East")
        self.deliver(self.move(11, (2, 101.62, 0)), self.move(12, (0, 98.62, 0)))
        nearest = self.entities.nearest_player(self.origin)
        self.assertIs(nearest, self.entities.get(11))
        self.assertAlmostEqual(nearest.position[1], 101.62, places=4)
        self.assertIs(self.entities.nearest_player(self.origin, lambda player: player.name == "Below"),
                      self.entities.get(12))
        self.assertIsNone(self.entities.nearest_player(self.origin, lambda _: False))

    def test_tie_uses_unique_id_and_unknown_identity_waits_for_the_list(self):
        self.deliver(self.listed(200, "First"), self.spawn(200, 11, "First", (2, 100, 0)),
                     self.spawn(100, 12, "Second", (-2, 100, 0)))
        self.assertEqual(self.entities.nearest_player(self.origin).entity_id, 200)
        self.deliver(self.listed(100, "Second"))
        self.assertEqual(self.entities.nearest_player(self.origin).entity_id, 100)
        self.assertIsNone(self.entities.nearest_player(None))

    def test_late_player_list_does_not_restore_a_passengers_old_feet(self):
        b = self.b
        for position_before_list in (False, True):
            with self.subTest(position_before_list=position_before_list):
                self.entities = Entities(SimpleNamespace(protocol=self.protocol), own_runtime_id=7, own_entity_id=1007)
                self.deliver(
                    self.spawn(101, 11, "Passenger", (1, 100, 0)),
                    self.spawn(102, 12, "Other", (2, 100, 0)),
                    b.AddActorPacket(entity_id=50, runtime_id=25, actor_type="minecraft:boat"),
                    b.SetActorLinkPacket(link=b.ActorLink(a=50, b=101, type=b.ActorLinkType.PASSENGER)),
                    b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                        runtime_id=25, pos=b.Vec3(x=30, y=100, z=0),
                    )),
                    b.SetActorLinkPacket(link=b.ActorLink(a=50, b=101, type=b.ActorLinkType.NONE)),
                )
                fresh = self.move(11, (0.5, 101.62, 0))
                if position_before_list:
                    self.deliver(fresh)
                self.deliver(self.listed(102, "Other"), self.listed(101, "Passenger"))
                self.assertEqual(self.entities.nearest_player(self.origin, lambda actor: actor.entity_id == 102).entity_id,
                                 102)
                self.assertEqual(self.entities.nearest_player(self.origin).entity_id,
                                 101 if position_before_list else 102)
                if not position_before_list:
                    self.assertIsNone(self.entities.nearest_player(self.origin, lambda actor: actor.entity_id == 101))
                    self.deliver(fresh)
                    self.assertEqual(self.entities.nearest_player(self.origin).entity_id, 101)

    def test_ignores_untrusted_feet_and_removes_actors_without_changing_membership(self):
        b = self.b
        self.deliver(self.listed(1007, "Self"), self.spawn(1007, 7, "Self", self.origin),
                     self.listed(101, "Remote"), self.spawn(101, 11, "Remote", (2, 100, 0)))
        self.deliver(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=11, pos=b.Vec3(x=2, y=100, z=0),
        )))
        self.assertIsNone(self.entities.nearest_player(self.origin))
        self.deliver(self.move(11, (2, 101.62, 0)))
        self.assertEqual(self.entities.nearest_player(self.origin).entity_id, 101)
        self.deliver(b.RemoveActorPacket(entity_id=101))
        self.assertIsNone(self.entities.nearest_player(self.origin))
        self.assertEqual({player.entity_id for player in self.entities.player_list()}, {101, 1007})

    def test_dimension_and_runtime_reuse_clear_candidate_feet(self):
        b = self.b
        self.deliver(self.listed(101, "Previous"), self.spawn(101, 11, "Previous", (2, 100, 0)),
                     b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.entities.nearest_player(self.origin))
        self.deliver(self.spawn(101, 12, "Previous", (2, 100, 0)))
        self.assertEqual(self.entities.nearest_player(self.origin).runtime_id, 12)
        self.deliver(self.spawn(102, 12, "Replacement", (1, 100, 0)))
        self.assertIsNone(self.entities.nearest_player(self.origin))
        self.deliver(self.listed(102, "Replacement"), b.PlayerListPacket(entries=[
            b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=UUID(int=101)),
        ]))
        self.assertEqual(self.entities.nearest_player(self.origin).entity_id, 102)
        self.deliver(b.DisconnectPacket())
        self.assertIsNone(self.entities.nearest_player(self.origin))
